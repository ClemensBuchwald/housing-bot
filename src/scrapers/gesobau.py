"""GESOBAU — Wohnungssuche.

Technischer Befund (Live-Prüfung 2026-06-05):
  - URL: https://www.gesobau.de/mieten/wohnungssuche
  - Server-seitig gerendert, 6 Angebote sichtbar im HTML
  - Klassen-Präfix: csm_ (TYPO3-CMS)
  - Aktuell KEINE Angebote in Charlottenburg-Wilmersdorf
  - Trotzdem pollbar — bei neuen CW-Angeboten sofort relevant

Geografischer Filter: geo.py — nur CW-Inserate werden weitergegeben
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper
from src.scrapers.geo import in_zielgebiet, extract_stadtteil

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.gesobau.de"
_SEARCH_URL = "https://www.gesobau.de/mieten/wohnungssuche"


class GESOBAUScraper(BaseScraper):
    name = "gesobau"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL)
        if resp is None:
            return []

        soup = self.parse(resp.text)

        # GESOBAU nutzt TYPO3, Klassen-Präfix csm_
        cards = (
            soup.select("li.wohnungsangebot")
            or soup.select(".wohnangebote li")
            or soup.select("[class*='expose']")
            or soup.select("[class*='wohnung']")
            or soup.select("article")
        )

        if not cards:
            logger.warning("[%s] Keine Inserate gefunden — Selektoren prüfen.", self.name)
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing and in_zielgebiet(listing):
                listings.append(listing)

        total = len(cards)
        matched = len(listings)
        if matched == 0:
            logger.info("[%s] %d Inserate gefunden, 0 im Zielgebiet CW.", self.name, total)
        else:
            logger.info("[%s] %d Inserate im Zielgebiet (von %d gesamt)", self.name, matched, total)
        return listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card.select_one("a[href]")
            if not link:
                return None
            href = link["href"]
            url = (_BASE_URL + href) if href.startswith("/") else href
            listing_id = url.rstrip("/").split("/")[-1][:60]

            titel_el = card.select_one("h2, h3, .title, [class*='title'], [class*='headline']")
            titel = titel_el.get_text(strip=True) if titel_el else "GESOBAU Wohnung"

            text = card.get_text(" ", strip=True)
            stadtteil = extract_stadtteil(titel + " " + text)

            warmmiete = _extract_price(text, ["Warmmiete", "Gesamtmiete"])
            kaltmiete = _extract_price(text, ["Kaltmiete", "Nettokaltmiete"])
            flaeche = _extract_qm(text)
            zimmer = _extract_zimmer(text)

            return Listing(
                id=f"gesobau-{listing_id}",
                portal="gesobau",
                url=url,
                titel=titel,
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=warmmiete,
                flaeche=flaeche,
                zimmer=zimmer,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _extract_price(text: str, labels: List[str]) -> Optional[float]:
    for label in labels:
        m = re.search(rf"{re.escape(label)}[:\s]*([0-9.]+(?:,[0-9]{{2}})?)\s*€?", text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _extract_qm(text: str) -> Optional[float]:
    m = re.search(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*m²", text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _extract_zimmer(text: str) -> Optional[float]:
    m = re.search(r"(\d(?:[.,]\d)?)\s*(?:Zimmer|Zi\.)", text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _to_float(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None
