"""Housing Bot — KI-gestützter Dauersuchassistent.

Zwei Threads:
  1. Telegram-Thread:  empfängt Aufträge und Befehle (getUpdates-Polling)
  2. Scraping-Thread:  läuft nur wenn aktiver Auftrag existiert

Starten:
  python -m src.main
  python -m src.main --once --mock   (Einmaliger Test mit Mock-Daten)
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from typing import List

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("housing_bot")

from src.config import load_criteria
from src.evaluator import evaluate_listing
from src.models import Listing
from src.notifications import NotificationService
from src.scrapers.base import BaseScraper
from src.scrapers.gesobau import GESOBAUScraper
from src.scrapers.inberlinwohnen import InBerlinWohnenScraper
from src.scrapers.mock import MockScraper
from src.scrapers.vonovia import VonoviaScraper
from src.scrapers.wbm import WBMScraper
from src.store import Store
from src.telegram_handler import TelegramHandler


def build_scrapers(use_mock: bool) -> List[BaseScraper]:
    if use_mock:
        return [MockScraper()]
    return [
        InBerlinWohnenScraper(),  # Tier 1: alle 6 Landeseigenen
        WBMScraper(),             # Tier 1: WBM direkt
        GESOBAUScraper(),         # Tier 1: GESOBAU direkt
        VonoviaScraper(),         # Tier 1: Vonovia/Deutsche Wohnen (JSON-API)
    ]


def _passes_basic(listing: Listing, crit: dict) -> bool:
    """Leichter Filter für On-Demand-Abfragen (ohne KI, nur harte Kriterien)."""
    preis = listing.warmmiete or listing.kaltmiete
    limit = crit.get("warmmiete_max") or crit.get("kaltmiete_max")
    if preis and limit and preis > float(limit):
        return False
    if crit.get("zimmer_min") and listing.zimmer and listing.zimmer < float(crit["zimmer_min"]):
        return False
    if crit.get("zimmer_max") and listing.zimmer and listing.zimmer > float(crit["zimmer_max"]):
        return False
    if crit.get("flaeche_min") and listing.flaeche and listing.flaeche < float(crit["flaeche_min"]):
        return False
    return True


def run_one_off_search(criteria: dict) -> List[dict]:
    """Einmalige Live-Abfrage ohne Auftrag/Persistenz. Gibt kompakte Treffer zurück.

    Nutzt schnelle Quellen + inberlinwohnen mit Seitenlimit, damit es zügig geht.
    """
    scrapers: List[BaseScraper] = [
        VonoviaScraper(),
        WBMScraper(),
        GESOBAUScraper(),
        InBerlinWohnenScraper(page_limit=4),
    ]
    yaml_criteria = load_criteria()
    treffer: List[dict] = []
    for scraper in scrapers:
        try:
            listings = scraper.fetch_listings(yaml_criteria)
        except Exception:
            logger.exception("On-Demand: Fehler bei %s", scraper.name)
            continue
        for l in listings:
            if not _passes_basic(l, criteria):
                continue
            treffer.append({
                "titel": l.titel,
                "ort": l.stadtteil or l.stadt,
                "warmmiete": l.warmmiete,
                "kaltmiete": l.kaltmiete,
                "flaeche": l.flaeche,
                "zimmer": l.zimmer,
                "quelle": l.portal,
                "url": l.url,
            })
    logger.info("On-Demand-Suche: %d Treffer", len(treffer))
    return treffer[:10]  # max 10 für eine lesbare Nachricht


def run_scraping_cycle(
    scrapers: List[BaseScraper],
    store: Store,
    notifier: NotificationService,
    mandate: dict,
) -> int:
    """Führt einen kompletten Scraping-Zyklus gegen den aktiven Auftrag durch."""
    hits = 0
    criteria = load_criteria()  # Hot-reload aus criteria.yaml

    for scraper in scrapers:
        logger.info("Scraper %s …", scraper.name)
        try:
            listings: List[Listing] = scraper.fetch_listings(criteria)
        except Exception:
            logger.exception("Fehler bei Scraper %s", scraper.name)
            continue

        logger.info("%d Inserate von %s", len(listings), scraper.name)

        for listing in listings:
            # Deduplizierung
            if store.is_known(listing.id, listing.portal):
                continue

            # Vollständige Daten persistieren
            store.save_listing(listing)

            # KI-Bewertung gegen aktiven Auftrag
            evaluation = evaluate_listing(listing, mandate)
            store.save_evaluation(listing.id, listing.portal, mandate.get("id", 0), evaluation.__dict__)

            if not evaluation.passt or evaluation.score < 30:
                logger.debug(
                    "Kein Match [%s] score=%d: %s",
                    listing.id, evaluation.score, evaluation.kurzfazit,
                )
                continue

            logger.info(
                "MATCH! [%s] '%s' — Score %d — %s",
                listing.id, listing.titel, evaluation.score, evaluation.empfehlung,
            )

            sent = notifier.send_evaluation(listing, evaluation)
            if sent:
                store.mark_notified(listing.id, listing.portal)
            hits += 1

    return hits


def scraping_thread(scrapers: List[BaseScraper], store: Store, notifier: NotificationService,
                    stop_event: threading.Event, poll_interval: int) -> None:
    """Hintergrund-Thread: scraped wenn aktiver Auftrag vorhanden."""
    logger.info("Scraping-Thread gestartet (Intervall: %ds)", poll_interval)
    while not stop_event.is_set():
        mandate = store.get_any_active_mandate()
        if mandate:
            logger.info("Aktiver Auftrag gefunden — starte Scraping-Zyklus")
            try:
                hits = run_scraping_cycle(scrapers, store, notifier, mandate)
                logger.info("%d neue Treffer in diesem Zyklus", hits)
            except Exception:
                logger.exception("Fehler im Scraping-Zyklus")
        else:
            logger.info("Kein aktiver Auftrag — Bot wartet")

        # Warte in kleinen Schritten, damit stop_event schnell reagiert
        for _ in range(poll_interval):
            if stop_event.is_set():
                break
            time.sleep(1)

    logger.info("Scraping-Thread beendet")


def telegram_thread(handler: TelegramHandler, stop_event: threading.Event) -> None:
    """Hintergrund-Thread: Telegram getUpdates-Polling."""
    logger.info("Telegram-Thread gestartet")
    while not stop_event.is_set():
        try:
            handler.poll_once()
        except Exception:
            logger.exception("Fehler im Telegram-Thread")
        time.sleep(2)
    logger.info("Telegram-Thread beendet")


def main() -> None:
    parser = argparse.ArgumentParser(description="Housing Bot")
    parser.add_argument("--once", action="store_true", help="Einmalig ausführen")
    parser.add_argument("--mock", action="store_true", help="Mock-Scraper")
    args = parser.parse_args()

    poll_interval = int(os.getenv("POLL_INTERVAL", "10")) * 60
    scrapers = build_scrapers(use_mock=args.mock)
    store = Store()
    notifier = NotificationService()
    # On-Demand-Suche nur im echten Betrieb (nicht im Mock-Modus)
    search_fn = None if args.mock else run_one_off_search
    tg_handler = TelegramHandler(store, search_fn=search_fn)

    logger.info(
        "Housing Bot gestartet. Scraper: %s. Mock: %s",
        [s.name for s in scrapers], args.mock,
    )

    if args.once:
        # Einmaliger Lauf: vorhandenen Auftrag nutzen oder Mock-Mandate
        mandate = store.get_any_active_mandate()
        if not mandate:
            logger.warning("Kein aktiver Auftrag — Einmallauf mit leerem Auftrag")
            mandate = {"raw_text": "Test", "structured": {}, "id": 0}
        hits = run_scraping_cycle(scrapers, store, notifier, mandate)
        logger.info("Einmaliger Lauf abgeschlossen. %d Treffer.", hits)
        store.close()
        return

    # Normalbetrieb: zwei Threads
    stop_event = threading.Event()

    t_scraping = threading.Thread(
        target=scraping_thread,
        args=(scrapers, store, notifier, stop_event, poll_interval),
        daemon=True,
        name="scraping",
    )
    t_telegram = threading.Thread(
        target=telegram_thread,
        args=(tg_handler, stop_event),
        daemon=True,
        name="telegram",
    )

    t_scraping.start()
    t_telegram.start()

    logger.info("Bot läuft. Warte auf Auftrag per Telegram. Stoppen mit Ctrl+C.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Bot wird gestoppt …")
        stop_event.set()
    finally:
        t_scraping.join(timeout=10)
        t_telegram.join(timeout=5)
        store.close()
        logger.info("Bot beendet.")


if __name__ == "__main__":
    main()
