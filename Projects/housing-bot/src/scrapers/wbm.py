"""WBM — Wohnungsbaugesellschaft Berlin-Mitte.

URL: https://www.wbm.de/wohnungen/mieten/

Technischer Ansatz:
  WBM rendert seine Angebotsliste server-seitig. Die Inserate sind direkt
  im HTML der Suchergebnisseite enthalten — kein JavaScript nötig.

  Selektoren (Stand 2025, ggf. nach Live-Test anpassen):
    Liste:   div.wbm-expose-list article  oder  .immolist-item
    Titel:   h3 oder .wbm-expose__title
    Preis:   .wbm-expose__price oder dd[data-label="Kaltmiete"]
    Fläche:  dd[data-label="Wohnfläche"]
    Zimmer:  dd[data-label="Zimmer"]
    Link:    a.wbm-expose__link oder article > a
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

_BASE_URL = "https://www.wbm.de"
_SEARCH_URL = "https://www.wbm.de/wohnungen/mieten/"


class WBMScraper(BaseScraper):
    name = "wbm"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL)
        if resp is None:
            return []

        soup = self.parse(resp.text)

        # Verschiedene bekannte Selektoren versuchen
        cards = (
            soup.select("article.wbm-expose")
            or soup.select(".immolist-item")
            or soup.select("article[class*='expose']")
            or soup.select(".wbm-expose-list article")
        )

        if not cards:
            logger.warning(
                "[%s] Keine Inserate gefunden. Selektoren möglicherweise veraltet — "
                "bitte HTML-Struktur auf wbm.de/wohnungen/mieten/ prüfen.",
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

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            # URL + ID
            link = card.select_one("a[href]")
            url = (_BASE_URL + link["href"]) if link and link["href"].startswith("/") else (link["href"] if link else _SEARCH_URL)
            listing_id = url.rstrip("/").split("/")[-1] or card.get("data-id", "")

            # Titel
            titel_el = card.select_one("h2, h3, .wbm-expose__title, [class*='title']")
            titel = titel_el.get_text(strip=True) if titel_el else "WBM Wohnung"

            # Stadtteil — oft im Titel oder separatem Feld
            stadtteil = self._extract_stadtteil(card, titel)

            # Preise
            kaltmiete = self._extract_price(card, ["Kaltmiete", "Nettokaltmiete", "kalt"])
            warmmiete = self._extract_price(card, ["Warmmiete", "Gesamtmiete", "warm"])

            # Fläche
            flaeche = self._extract_number(card, ["Wohnfläche", "Fläche", "qm", "m²"])

            # Zimmer
            zimmer = self._extract_number(card, ["Zimmer", "Zi."])

            return Listing(
                id=f"wbm-{listing_id}",
                portal="wbm",
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

    def _extract_price(self, card, labels: List[str]) -> Optional[float]:
        for label in labels:
            # data-label Attribut
            el = card.select_one(f'dd[data-label*="{label}"], dt:-soup-contains("{label}") + dd')
            if el:
                return self._to_float(el.get_text())
            # Text-Suche
            for el in card.select("dd, span, td"):
                prev = el.find_previous_sibling()
                if prev and label.lower() in prev.get_text(strip=True).lower():
                    return self._to_float(el.get_text())
        return None

    def _extract_number(self, card, labels: List[str]) -> Optional[float]:
        return self._extract_price(card, labels)

    def _extract_stadtteil(self, card, titel: str) -> Optional[str]:
        # Bekannte Berliner Stadtteile im Titel suchen
        stadtteile = [
            "Mitte", "Prenzlauer Berg", "Friedrichshain", "Kreuzberg",
            "Charlottenburg", "Wilmersdorf", "Halensee", "Grunewald",
            "Schöneberg", "Tempelhof", "Neukölln", "Treptow", "Pankow",
            "Weißensee", "Lichtenberg", "Marzahn", "Hellersdorf",
            "Spandau", "Reinickendorf", "Steglitz", "Zehlendorf",
        ]
        text = titel + " " + card.get_text(" ")
        for st in stadtteile:
            if st.lower() in text.lower():
                return st
        return None

    @staticmethod
    def _to_float(text: str) -> Optional[float]:
        if not text:
            return None
        try:
            cleaned = re.sub(r"[^\d,.]", "", text).replace(".", "").replace(",", ".")
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
