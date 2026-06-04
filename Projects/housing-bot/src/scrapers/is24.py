"""ImmobilienScout24 Scraper — Stub für Phase 1.

Wird in Phase 2 implementiert. Benötigt IS24_API_KEY in .env.
API-Doku: https://api.immobilienscout24.de/
"""
from __future__ import annotations

import logging

from typing import List

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class IS24Scraper(BaseScraper):
    name = "is24"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        logger.warning(
            "IS24Scraper ist noch nicht implementiert (Phase 2). "
            "Setze IS24_API_KEY in .env und implementiere src/scrapers/is24.py."
        )
        return []
