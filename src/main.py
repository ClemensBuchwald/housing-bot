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
import re
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
from src.agent import build_sources_text
from src.scrapers.base import BaseScraper
from src.scrapers.degewo import DegewoScraper
from src.scrapers.gesobau import GESOBAUScraper
from src.scrapers.gewobag import GewobagScraper
from src.scrapers.immowelt import ImmoweltScraper
from src.scrapers.inberlinwohnen import InBerlinWohnenScraper
from src.scrapers.is24 import IS24Scraper
from src.scrapers.is24_detail import enrich_listing, get_contact
from src.scrapers.kleinanzeigen import KleinanzeigenScraper
from src.scrapers.mock import MockScraper
from src.scrapers.vonovia import VonoviaScraper
from src.scrapers.wbm import WBMScraper
from src.store import Store
from src.telegram_handler import TelegramHandler


def build_scrapers(use_mock: bool) -> List[BaseScraper]:
    """Reihenfolge = Meldegeschwindigkeit.

    Der Zyklus läuft sequenziell, also bestimmt die Position, wie alt ein Inserat
    beim Alert schon ist. Gemessen: inberlinwohnen ~61 s, degewo ~51 s — liefen sie
    zuerst, wurde IS24 (die ergiebigste Quelle, ~11 s) erst nach ~2,5 min abgefragt.
    Daher: schnelle, ertragreiche Quellen zuerst, träge Quellen ans Ende.
    """
    if use_mock:
        return [MockScraper()]
    return [
        # --- Schnell + hohe Ausbeute: zuerst, damit Alerts früh rausgehen ---
        IS24Scraper(),            # ~11 s, größte Quelle (Mobile-API, CW-Geocodes)
        ImmoweltScraper(),        # ~15 s, großes Portal (CW-Ortsteilsuche)
        VonoviaScraper(),         # ~11 s, Vonovia/Deutsche Wohnen (JSON-API)
        # --- Schnell, aber selten Treffer ---
        WBMScraper(),             # ~8 s
        GESOBAUScraper(),         # ~6 s
        GewobagScraper(),         # ~6 s
        # --- Träge: ans Ende, verzögern so niemanden ---
        DegewoScraper(),          # ~51 s (TYPO3-HTML)
        KleinanzeigenScraper(),   # ~40 s (3 Seiten je Ort)
        InBerlinWohnenScraper(),  # ~61 s (alle 6 Landeseigenen, viele Seiten)
        # Heimstaden + GCP deaktiviert: Heimstaden ist ein IS24-iframe-Widget (Duplikat),
        # GCP über Playwright nicht zuverlässig filterbar + 0 CW-Ausbeute.
        # Playwright-Basis bleibt für künftige echte JS-Quellen (siehe docs/sources.md).
    ]


# Angebotsarten, die bei einer Dauermiet-Suche nie passen. Sie machen einen
# erheblichen Teil der Rohtreffer aus (gemessen: 14% Tauschwohnungen) und wurden
# bisher teuer von der KI aussortiert.
_IMMER_AUSGESCHLOSSEN = [
    "tauschwohnung", "wohnungstausch", "tausche wohnung", "tausch gegen",
    "zwischenmiete", "auf zeit",
]

# "möbliert" darf NICHT blind blocken — es steckt auch in "unmöbliert",
# und genau das ist ja gewünscht. Daher negative Vorprüfung.
_MOEBLIERT_RE = re.compile(r"(?<!un)(?<!nicht )m[öo]bliert", re.IGNORECASE)


def _hat_ausschlusswort(listing: Listing, crit: dict) -> Optional[str]:
    """Prüft Titel + Merkmale gegen Ausschlusswörter. Gibt das Treffer-Wort zurück."""
    text = (listing.titel or "").lower() + " " + " ".join(str(m) for m in (listing.merkmale or [])).lower()
    for w in _IMMER_AUSGESCHLOSSEN + [str(w).lower() for w in (crit.get("ausschlusskriterien") or [])]:
        if w and w in text:
            return w
    if _MOEBLIERT_RE.search(text) and "unmöbliert" not in text and "unmobliert" not in text:
        return "möbliert"
    return None


def _passes_basic(listing: Listing, crit: dict) -> bool:
    """Regelbasierter Vorfilter — läuft VOR der KI und kostet nichts.

    Prüft harte Zahlen (Preis/Zimmer/Fläche) und Ausschlusswörter. Fehlende
    Werte führen NICHT zur Ablehnung — im Zweifel entscheidet die KI.
    """
    if _hat_ausschlusswort(listing, crit):
        return False
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

    # 1b) Detaildaten nachladen (Etage/Balkon/Keller/Aufzug/Beschreibung),
    #     damit die KI Kriterien wie "kein Erdgeschoss" wirklich prüfen kann
    enrich_candidates(candidates)

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


def enrich_candidates(listings: List[Listing]) -> int:
    """Lädt Detaildaten nach (aktuell IS24): Etage, Balkon, Keller, Aufzug, Beschreibung.

    Ohne diese Daten kann die KI Kriterien wie "kein Erdgeschoss" oder "Balkon"
    nicht belastbar prüfen. Fehler sind unkritisch — Listing bleibt dann roh.
    """
    n = 0
    for l in listings:
        try:
            if enrich_listing(l):
                n += 1
        except Exception:
            logger.debug("Enrichment fehlgeschlagen für %s", l.id, exc_info=True)
    if n:
        logger.info("Detaildaten für %d/%d Inserate nachgeladen", n, len(listings))
    return n


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


# Schutz vor Nachrichtenflut: Wenn eine Quelle neu dazukommt, sind schlagartig
# hunderte Inserate "neu". Pro Zyklus werden daher nur begrenzt viele bewertet
# und gemeldet — der Rest bleibt ungesehen und kommt im nächsten Zyklus dran.
MAX_EVAL_PRO_ZYKLUS = int(os.getenv("MAX_EVAL_PRO_ZYKLUS", "40"))
MAX_ALERTS_PRO_ZYKLUS = int(os.getenv("MAX_ALERTS_PRO_ZYKLUS", "8"))
# Damit eine grosse Quelle (IS24: 150 Inserate) bei Rückstau nicht das ganze
# Budget frisst und die übrigen Quellen aushungert.
MAX_EVAL_PRO_QUELLE = int(os.getenv("MAX_EVAL_PRO_QUELLE", "15"))


def run_scraping_cycle(
    scrapers: List[BaseScraper],
    store: Store,
    notifier: NotificationService,
    mandate: dict,
) -> int:
    """Führt einen kompletten Scraping-Zyklus gegen den aktiven Auftrag durch."""
    hits = 0
    evaluated = 0
    criteria = load_criteria()  # Hot-reload aus criteria.yaml

    for scraper in scrapers:
        if evaluated >= MAX_EVAL_PRO_ZYKLUS:
            break
        logger.info("Scraper %s …", scraper.name)
        try:
            listings: List[Listing] = scraper.fetch_listings(criteria)
        except Exception:
            logger.exception("Fehler bei Scraper %s", scraper.name)
            continue

        logger.info("%d Inserate von %s", len(listings), scraper.name)
        eval_quelle = 0

        for listing in listings:
            if evaluated >= MAX_EVAL_PRO_ZYKLUS:
                logger.info("Bewertungslimit (%d) erreicht — Rest folgt im nächsten Zyklus",
                            MAX_EVAL_PRO_ZYKLUS)
                break
            if eval_quelle >= MAX_EVAL_PRO_QUELLE:
                logger.info("[%s] Quellen-Limit (%d) erreicht — Rest folgt im nächsten Zyklus",
                            scraper.name, MAX_EVAL_PRO_QUELLE)
                break

            # Deduplizierung
            if store.is_known(listing.id, listing.portal):
                continue

            krit = mandate.get("structured") or {}

            # Frühfilter auf den bereits vorhandenen Feldern (Zimmer/Fläche/Kaltmiete):
            # spart den Detail-Request UND die KI-Bewertung für offensichtliche Ausreißer.
            if not _passes_basic(listing, krit):
                store.save_listing(listing)   # trotzdem als gesehen merken
                logger.debug("Frühfilter [%s]: harte Kriterien verfehlt", listing.id)
                continue

            # Detaildaten nachladen (Etage/Balkon/Keller/Aufzug/Warmmiete) — erst NACH
            # Dedup und Frühfilter, damit nur relevante Inserate einen Request kosten
            try:
                enrich_listing(listing)
            except Exception:
                logger.debug("Enrichment fehlgeschlagen für %s", listing.id, exc_info=True)

            # Vollständige Daten persistieren
            store.save_listing(listing)

            # Zweiter Durchgang: jetzt ist auch die Warmmiete bekannt (kam erst
            # aus dem Detail-Abruf) — ohne KI-Kosten erneut hart prüfen.
            if not _passes_basic(listing, krit):
                logger.debug("Vorfilter [%s]: Warmmiete/Kriterien verfehlt", listing.id)
                continue

            # KI-Bewertung gegen aktiven Auftrag
            evaluation = evaluate_listing(listing, mandate)
            evaluated += 1
            eval_quelle += 1
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
            hits += 1

            # Nur begrenzt viele Alerts pro Zyklus — sonst flutet ein neu
            # angebundenes Portal den Chat. Bereits als gesehen gespeichert,
            # daher keine Wiederholung; Fund steht im Audit-Log.
            if hits > MAX_ALERTS_PRO_ZYKLUS:
                continue

            sent = notifier.send_evaluation(listing, evaluation)
            if sent:
                store.mark_notified(listing.id, listing.portal)

    if hits > MAX_ALERTS_PRO_ZYKLUS:
        rest = hits - MAX_ALERTS_PRO_ZYKLUS
        logger.info("%d weitere Treffer nicht einzeln gemeldet (Limit %d)", rest, MAX_ALERTS_PRO_ZYKLUS)
        notifier.send_text(
            f"… und {rest} weitere passende Angebote in diesem Durchlauf.\n"
            f"Sag „zeig mir alle“, wenn ihr sie sehen wollt."
        )

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
    sources_text = build_sources_text([s.name for s in scrapers])
    tg_handler = TelegramHandler(store, search_fn=search_fn, sources_text=sources_text,
                                 contact_fn=get_contact)

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
