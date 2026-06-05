"""Charlottenburger Baugenossenschaft (CBG) — cbg-berlin.de

URL: https://www.cbg-berlin.de/wohnungen/

Technischer Ansatz:
  Kleine Genossenschaft, einfache WordPress-Seite.
  Wohnungsangebote werden als statische Seiten/Beiträge gepflegt.
  Kein dynamisches Laden nötig.

  Selektoren (nach Live-Test ggf. anpassen):
    Angebote: .entry-content, article.post, .wohnungsangebot
    Alternativ: direkte Unterseite /wohnungen/ mit Liste
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.cbg-berlin.de"
_SEARCH_URL = "https://www.cbg-berlin.de/wohnungen/"


class CBGScraper(BaseScraper):
    name = "cbg"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL)
        if resp is None:
            return []

        soup = self.parse(resp.text)

        # CBG nutzt wahrscheinlich einfache Post-Listen oder Tabellen
        cards = (
            soup.select("article.wohnungsangebot")
            or soup.select(".wohnangebote article")
            or soup.select("article.post")
            or soup.select(".entry-content table tr")
        )

        # Fallback: alle Links auf der Seite die auf /wohnungen/ zeigen
        if not cards:
            links = soup.select(f'a[href*="{_BASE_URL}/wohnungen/"]') or soup.select('a[href*="/wohnungen/"]')
            links = [l for l in links if l["href"] != _SEARCH_URL and "/wohnungen/" in l["href"]]
            if links:
                logger.info("[%s] Keine Karten, aber %d Einzel-Links gefunden", self.name, len(links))
                return self._fetch_detail_pages(links[:20])  # max 20 Seiten
            logger.warning(
                "[%s] Keine Inserate gefunden. "
                "Bitte cbg-berlin.de/wohnungen/ manuell prüfen und Selektoren anpassen.",
                self.name,
            )
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing:
                listings.append(listing)

        logger.info("[%s] %d Inserate gefunden", self.name, len(listings))
        return listings

    def _fetch_detail_pages(self, links) -> List[Listing]:
        """Ruft Einzel-Detailseiten ab wenn keine Listenansicht vorhanden."""
        listings = []
        for link in links:
            url = link["href"]
            if not url.startswith("http"):
                url = _BASE_URL + url
            resp = self.get(url)
            if resp is None:
                continue
            soup = self.parse(resp.text)
            listing = self._parse_detail_page(soup, url)
            if listing:
                listings.append(listing)
        return listings

    def _parse_detail_page(self, soup, url: str) -> Optional[Listing]:
        try:
            titel_el = soup.select_one("h1, h2.entry-title, .page-title")
            titel = titel_el.get_text(strip=True) if titel_el else "CBG Wohnung"
            listing_id = url.rstrip("/").split("/")[-1]
            text = soup.get_text(" ")

            kaltmiete = self._extract_price_from_text(text, ["Kaltmiete", "Nettokaltmiete"])
            warmmiete = self._extract_price_from_text(text, ["Warmmiete", "Gesamtmiete"])
            flaeche = self._extract_qm_from_text(text)
            zimmer = self._extract_zimmer_from_text(text)

            return Listing(
                id=f"cbg-{listing_id}",
                portal="cbg",
                url=url,
                titel=titel,
                stadt="Berlin",
                stadtteil="Charlottenburg",  # CBG ist ausschließlich in Charlottenburg
                kaltmiete=kaltmiete,
                warmmiete=warmmiete,
                flaeche=flaeche,
                zimmer=zimmer,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card.select_one("a[href]")
            url = link["href"] if link else _SEARCH_URL
            if url.startswith("/"):
                url = _BASE_URL + url
            titel_el = card.select_one("h2, h3, .title")
            titel = titel_el.get_text(strip=True) if titel_el else "CBG Wohnung"
            listing_id = url.rstrip("/").split("/")[-1]
            text = card.get_text(" ")
            return Listing(
                id=f"cbg-{listing_id}",
                portal="cbg",
                url=url,
                titel=titel,
                stadt="Berlin",
                stadtteil="Charlottenburg",
                kaltmiete=self._extract_price_from_text(text, ["Kaltmiete"]),
                warmmiete=self._extract_price_from_text(text, ["Warmmiete"]),
                flaeche=self._extract_qm_from_text(text),
                zimmer=self._extract_zimmer_from_text(text),
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None

    @staticmethod
    def _extract_price_from_text(text: str, labels: List[str]) -> Optional[float]:
        for label in labels:
            match = re.search(rf"{label}[:\s]+([0-9.,]+)\s*€?", text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1).replace(".", "").replace(",", "."))
                except ValueError:
                    pass
        # Fallback: erster Euro-Betrag im Text
        match = re.search(r"(\d{3,4}(?:[.,]\d{2})?)\s*€", text)
        if match:
            try:
                return float(match.group(1).replace(".", "").replace(",", "."))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_qm_from_text(text: str) -> Optional[float]:
        match = re.search(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*m²", text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_zimmer_from_text(text: str) -> Optional[float]:
        match = re.search(r"(\d(?:[.,]\d)?)\s*(?:Zimmer|Zi\.)", text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                pass
        return None
