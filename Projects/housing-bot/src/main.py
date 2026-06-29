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
import functools
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
from src.scrapers.degewo import DegewoScraper
from src.scrapers.gcp import GCPScraper
from src.scrapers.gesobau import GESOBAUScraper
from src.scrapers.gewobag import GewobagScraper
from src.scrapers.heimstaden import HeimstadenScraper
from src.scrapers.immowelt import ImmoweltScraper
from src.scrapers.inberlinwohnen import InBerlinWohnenScraper
from src.scrapers.is24 import IS24Scraper
from src.scrapers.kleinanzeigen import KleinanzeigenScraper
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
        GewobagScraper(),         # Tier 1: Gewobag direkt (CW-Bezirksfilter)
        DegewoScraper(),          # Tier 1: degewo (TYPO3-HTML, kein Playwright nötig)
        IS24Scraper(),            # Tier 1: ImmobilienScout24 (Mobile-API, CW-Geocodes)
        ImmoweltScraper(),        # Tier 2: Immowelt (großes Portal, CW-Ortsteilsuche)
        KleinanzeigenScraper(),   # Tier 2: Kleinanzeigen (private + Makler)
        HeimstadenScraper(),      # Tier 3: Heimstaden (JS, Playwright-Basis)
        GCPScraper(),             # Tier 3: Grand City Property (JS, Playwright + Ortssuche)
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


def run_one_off_search(criteria: dict, mandate: Optional[dict] = None,
                       store: "Optional[Store]" = None, include_seen: bool = False) -> List[dict]:
    """Einmalige Live-Abfrage MIT KI-Bewertung gegen den Auftrag.

    Ablauf: scrapen → Grobfilter (Preis/Zimmer/Fläche) → bereits Gesehenes überspringen
    (außer include_seen=True) → KI bewertet jeden Kandidaten → nur passende, nach Score
    sortiert → als gesehen merken.

    include_seen=True: zeigt ALLE passenden inkl. bereits gezeigter ("zeig mir nochmal alle").
    """
    scrapers: List[BaseScraper] = [
        VonoviaScraper(),
        WBMScraper(),
        GESOBAUScraper(),
        GewobagScraper(),
        IS24Scraper(),
        ImmoweltScraper(),
        KleinanzeigenScraper(),
        InBerlinWohnenScraper(page_limit=4),
    ]
    yaml_criteria = load_criteria()

    # 1) Kandidaten einsammeln + Grobfilter
    candidates: List[Listing] = []
    for scraper in scrapers:
        try:
            listings = scraper.fetch_listings(yaml_criteria)
        except Exception:
            logger.exception("On-Demand: Fehler bei %s", scraper.name)
            continue
        for l in listings:
            if not _passes_basic(l, criteria):
                continue
            # Bereits Gesehenes überspringen — außer der Nutzer will explizit ALLE
            if store and not include_seen and store.is_known(l.id, l.portal):
                continue
            candidates.append(l)

    candidates = candidates[:12]  # KI-Budget begrenzen
    logger.info("On-Demand: %d neue Kandidaten → KI-Bewertung", len(candidates))

    # 2) KI-Bewertung gegen den Auftrag
    eval_mandate = mandate or {"raw_text": _criteria_to_text(criteria), "structured": criteria}
    treffer: List[dict] = []
    for l in candidates:
        try:
            ev = evaluate_listing(l, eval_mandate)
        except Exception:
            logger.exception("On-Demand: Bewertung fehlgeschlagen für %s", l.id)
            continue
        if not ev.passt or ev.score < 40:
            continue
        # Als gesehen/gemeldet merken → kein Doppel in Dauersuche oder nächster Sofort-Abfrage
        if store:
            try:
                store.save_listing(l)
                store.save_evaluation(l.id, l.portal, (mandate or {}).get("id", 0), ev.__dict__)
                store.mark_notified(l.id, l.portal)
            except Exception:
                logger.debug("On-Demand: Persistenz fehlgeschlagen für %s", l.id, exc_info=True)
        treffer.append({
            "titel": l.titel,
            "ort": l.stadtteil or l.stadt,
            "warmmiete": l.warmmiete,
            "kaltmiete": l.kaltmiete,
            "flaeche": l.flaeche,
            "zimmer": l.zimmer,
            "quelle": l.portal,
            "url": l.url,
            "score": ev.score,
            "vorteile": ev.vorteile[:2],
            "nachteile": (ev.nachteile or ev.offene_punkte)[:2],
            "empfehlung": ev.empfehlung,
        })

    treffer.sort(key=lambda t: -t["score"])
    logger.info("On-Demand: %d passende Treffer nach KI-Bewertung", len(treffer))
    return treffer[:8]


def _criteria_to_text(c: dict) -> str:
    parts = []
    if c.get("zielorte"):
        parts.append("in " + " oder ".join(c["zielorte"]))
    if c.get("zimmer_min"):
        parts.append(f"ab {c['zimmer_min']} Zimmer")
    if c.get("flaeche_min"):
        parts.append(f"ab {c['flaeche_min']} m²")
    if c.get("warmmiete_max"):
        parts.append(f"max {c['warmmiete_max']} € warm")
    if c.get("ausschlusskriterien"):
        parts.append("ohne: " + ", ".join(c["ausschlusskriterien"]))
    if c.get("wunschkriterien"):
        parts.append("Wunsch: " + ", ".join(c["wunschkriterien"]))
    return "Wohnungssuche " + ", ".join(parts) if parts else "Wohnungssuche"


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
    # Sofort-Suche mit Store verbinden (Dedup + Persistenz der gezeigten Treffer)
    search_fn = None if args.mock else functools.partial(run_one_off_search, store=store)
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
