"""WBM — Wohnungsbaugesellschaft Berlin-Mitte.

Technischer Befund (Live-Analyse 2026-06-05):
  - URL: https://www.wbm.de/wohnungen-berlin/angebote/
  - Server-seitig gerendert, 15 Angebote
  - Listing-Container: article.immo-element
  - Preis: direkt als Text "1.558,04 €\\nWarmmiete"
  - Größe: "94,92 m²\\nGröße"
  - Zimmer: "3\\nZimmer"
  - Detail-Link: a[href*='/angebote/details/']
  - Aktuell 0 Angebote in CW (Schwerpunkt Spandau/Lichtenberg)

Geografischer Filter: geo.py
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

_BASE_URL = "https://www.wbm.de"
_SEARCH_URL = "https://www.wbm.de/wohnungen-berlin/angebote/"


class WBMScraper(BaseScraper):
    name = "wbm"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL)
        if resp is None:
            return []

        soup = self.parse(resp.text)
        cards = soup.select("article.immo-element")

        if not cards:
            logger.warning("[%s] Keine article.immo-element gefunden auf %s", self.name, _SEARCH_URL)
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing and in_zielgebiet(listing):
                listings.append(listing)

        logger.info("[%s] %d Inserate im Zielgebiet (von %d gesamt)", self.name, len(listings), len(cards))
        return listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            # Detail-Link
            link = card.select_one("a[href*='/angebote/details/']") or card.select_one("a[href]")
            href = link["href"] if link else ""
            url = (_BASE_URL + href) if href.startswith("/") else href
            listing_id = url.rstrip("/").split("/")[-1].split("?")[-1][:60] or re.sub(r"\W", "-", url)[-50:]

            # Titel (z.B. "3-Zimmer-Wohnung in Spandau")
            titel_el = card.select_one("h2, h3, .textWrap h2, .textWrap h3")
            titel = titel_el.get_text(strip=True) if titel_el else "WBM Wohnung"

            # Adresse
            addr_el = card.select_one(".address, .adresse, address, [class*='address']")
            addr_text = addr_el.get_text(strip=True) if addr_el else ""
            stadtteil = extract_stadtteil(titel + " " + addr_text)

            # Gesamten Text für Regex-Extraktion
            full_text = card.get_text(" ", strip=True)

            # Warmmiete: "1.558,04 € Warmmiete" oder "Warmmiete 1.558,04 €"
            warmmiete = _price_near_label(full_text, "Warmmiete")
            kaltmiete = _price_near_label(full_text, "Kaltmiete")

            # Fläche: "94,92 m² Größe"
            flaeche_m = re.search(r"([\d]+[.,][\d]+)\s*m²", full_text)
            flaeche = _to_float(flaeche_m.group(1)) if flaeche_m else None

            # Zimmer: "3 Zimmer" oder "Zimmer 3"
            zimmer_m = re.search(r"(\d(?:[.,]\d)?)\s*Zimmer|Zimmer\s*(\d(?:[.,]\d)?)", full_text)
            zimmer = _to_float((zimmer_m.group(1) or zimmer_m.group(2))) if zimmer_m else None

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


def _price_near_label(text: str, label: str) -> Optional[float]:
    patterns = [
        rf"([\d.]+,\d{{2}})\s*€\s*{label}",
        rf"{label}\s*([\d.]+,\d{{2}})\s*€",
        rf"{label}[:\s]+([\d.]+,\d{{2}})",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(str(s).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None
