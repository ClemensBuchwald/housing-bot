from __future__ import annotations

import logging
import time
import random
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from src.config import Criteria
from src.models import Listing

logger = logging.getLogger(__name__)

# Gemeinsamer User-Agent — wirkt wie ein normaler Browser
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class BaseScraper(ABC):
    name: str = "base"

    # Fehlgeschlagene Abrufe seit dem letzten Zurücksetzen.
    #
    # Hintergrund: get() gibt bei erschöpften Versuchen None zurück, und die
    # Scraper machen daraus eine leere Liste. Von aussen sah ein totes Portal
    # damit exakt aus wie ein ruhiger Markt. Der Zähler macht den Unterschied
    # sichtbar, ohne dass ein Scraper angefasst werden muss.
    #
    # Als Klassenattribut angelegt, damit Unterklassen mit eigenem __init__
    # (z. B. InBerlinWohnenScraper) nichts aufrufen müssen; die Zuweisung in
    # get() erzeugt dann das Instanzattribut.
    _abruf_fehler: int = 0

    def abrufe_zuruecksetzen(self) -> None:
        """Vor jedem Zyklus aufrufen."""
        self._abruf_fehler = 0

    @property
    def hatte_abruf_fehler(self) -> bool:
        return self._abruf_fehler > 0

    @abstractmethod
    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        """Fetch raw listings from the portal. Must be implemented per portal."""
        ...

    # --- HTTP-Hilfsmethoden ---

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = 15,
        retries: int = 3,
        min_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
    ) -> Optional[httpx.Response]:
        """HTTP GET mit Retry und höflicher Pause."""
        hdrs = {**DEFAULT_HEADERS, **(headers or {})}
        for attempt in range(1, retries + 1):
            try:
                resp = httpx.get(url, params=params, headers=hdrs, timeout=timeout, follow_redirects=True)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 30))
                    logger.warning("[%s] Rate limit, warte %ds", self.name, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                self._hoefliche_pause(min_delay, max_delay)
                return resp
            except httpx.HTTPError as e:
                logger.warning("[%s] HTTP-Fehler (Versuch %d/%d): %s", self.name, attempt, retries, e)
                if attempt < retries:
                    time.sleep(2 ** attempt)
        logger.error("[%s] Kein Response nach %d Versuchen: %s", self.name, retries, url)
        self._abruf_fehler = self._abruf_fehler + 1
        return None

    def parse(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def _hoefliche_pause(self, min_s: Optional[float] = None, max_s: Optional[float] = None) -> None:
        """Zufällige Pause zwischen Requests — reduziert Blocking-Risiko."""
        import os
        if min_s is None:
            min_s = float(os.getenv("SCRAPE_DELAY_MIN", "3"))
        if max_s is None:
            max_s = float(os.getenv("SCRAPE_DELAY_MAX", "8"))
        time.sleep(random.uniform(min_s, max_s))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
