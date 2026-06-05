"""Housing Bot — Hauptprogramm.

Startet mit:
    python -m src.main

Einmalig (kein Loop):
    python -m src.main --once
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from dotenv import load_dotenv

from src.config import load_criteria
from src.matching import match
from src.models import Listing
from src.notifications import NotificationService
from typing import List

from src.scrapers.base import BaseScraper
from src.scrapers.cbg import CBGScraper
from src.scrapers.gesobau import GESOBAUScraper
from src.scrapers.inberlinwohnen import InBerlinWohnenScraper
from src.scrapers.mock import MockScraper
from src.scrapers.wbm import WBMScraper
from src.store import Store

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("housing_bot")


def build_scrapers(use_mock: bool) -> List[BaseScraper]:
    if use_mock:
        return [MockScraper()]
    return [
        InBerlinWohnenScraper(),  # Tier 1: alle 6 Landeseigenen, mit CW-Filter
        WBMScraper(),             # Tier 1: WBM direkt (eigene URL, Ergänzung)
        GESOBAUScraper(),         # Tier 1: GESOBAU (aktuell selten CW, aber pollbar)
        CBGScraper(),             # Tier 2: Charlottenburger Baugenossenschaft
    ]


def run_once(scrapers: List[BaseScraper], store: Store, notifier: NotificationService) -> int:
    criteria = load_criteria()  # frisch laden — ermöglicht Hot-Reload ohne Neustart
    hits = 0

    for scraper in scrapers:
        logger.info("Scraper %s wird ausgeführt …", scraper.name)
        try:
            listings: List[Listing] = scraper.fetch_listings(criteria)
        except Exception:
            logger.exception("Fehler bei Scraper %s", scraper.name)
            continue

        logger.info("%d Inserate von %s abgerufen", len(listings), scraper.name)

        for listing in listings:
            if criteria.benachrichtigung.nur_neue and store.is_known(listing.id, listing.portal):
                continue

            # Vollständige Listing-Daten persistieren (Audit-Trail)
            store.save_listing(listing)

            result = match(listing, criteria)

            # Match-Ergebnis persistieren (inkl. Ablehnungsgrund)
            store.save_match(result, geo_ok=True)

            if not result.bestanden:
                logger.debug(
                    "Kein Match [%s] '%s': %s",
                    listing.id, listing.titel, result.ablehnungsgrund,
                )
                continue

            logger.info(
                "Match! [%s] '%s' — Score %d — %s",
                listing.id, listing.titel, result.score, listing.stadtteil or listing.stadt,
            )
            sent = notifier.send(result)
            if sent:
                store.mark_notified(listing.id, listing.portal)
            hits += 1

    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="Housing Bot")
    parser.add_argument("--once", action="store_true", help="Einmalig ausführen, kein Loop")
    parser.add_argument("--mock", action="store_true", help="Mock-Scraper verwenden")
    args = parser.parse_args()

    poll_interval = int(os.getenv("POLL_INTERVAL", "10")) * 60

    scrapers = build_scrapers(use_mock=args.mock)
    store = Store()
    notifier = NotificationService()

    logger.info(
        "Housing Bot gestartet. Scraper: %s. Intervall: %ds. Mock: %s",
        [s.name for s in scrapers],
        poll_interval,
        args.mock,
    )

    try:
        if args.once:
            hits = run_once(scrapers, store, notifier)
            logger.info("Einmaliger Lauf abgeschlossen. %d Treffer.", hits)
        else:
            while True:
                hits = run_once(scrapers, store, notifier)
                logger.info("%d Treffer in diesem Durchlauf. Nächster in %ds.", hits, poll_interval)
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Bot gestoppt.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
