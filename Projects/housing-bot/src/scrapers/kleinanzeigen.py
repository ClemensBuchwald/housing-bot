"""eBay Kleinanzeigen (kleinanzeigen.de) — Wohnungssuche.

Technischer Befund (Live-Analyse 2026-06-05):
  - URL: https://www.kleinanzeigen.de/s-wohnung-mieten/{ort}/c203...
  - Server-seitig gerendert, 200 OK ohne Login
  - Stabile Selektoren:
      Container: article.aditem (data-adid)
      Preis:     .aditem-main--middle--price-shipping--price
      Ort:       .aditem-main--top--left  (enthält PLZ)
      Titel/Link: h2 a → /s-anzeige/...
  - Zimmer/Fläche meist im Titel oder Tags

Geografischer Filter: geo.py (PLZ/Ortsteil im Ort-Feld)
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

_BASE_URL = "https://www.kleinanzeigen.de"
# Berlin-weite Mietwohnungssuche; geo.py filtert auf CW
_SEARCH_URLS = [
    "https://www.kleinanzeigen.de/s-wohnung-mieten/charlottenburg/c203l3331",
    "https://www.kleinanzeigen.de/s-wohnung-mieten/wilmersdorf/c203l3333",
]


class KleinanzeigenScraper(BaseScraper):
    name = "kleinanzeigen"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        all_listings: List[Listing] = []
        seen = set()

        for url in _SEARCH_URLS:
            resp = self.get(url, min_delay=3.0, max_delay=6.0)
            if resp is None:
                continue
            soup = self.parse(resp.text)
            cards = soup.select("article.aditem")
            if not cards:
                logger.debug("[%s] Keine Karten auf %s", self.name, url)
                continue

            for card in cards:
                listing = self._parse_card(card)
                if listing and listing.id not in seen and in_zielgebiet(listing):
                    seen.add(listing.id)
                    all_listings.append(listing)

        logger.info("[%s] %d Inserate im Zielgebiet CW", self.name, len(all_listings))
        return all_listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            adid = card.get("data-adid", "")
            # Titel-Link gezielt: h2.text-module-begin a (nicht der Bild-Link mit Zähler)
            link_el = card.select_one("h2.text-module-begin a, h2 a.ellipsis, a.ellipsis")
            href = link_el["href"] if link_el else ""
            url = (_BASE_URL + href) if href.startswith("/") else href
            titel = link_el.get_text(strip=True) if link_el else "Wohnung"

            ort_el = card.select_one(".aditem-main--top--left")
            ort_text = ort_el.get_text(strip=True) if ort_el else ""
            plz_m = re.search(r"\b(\d{5})\b", ort_text)
            plz = plz_m.group(1) if plz_m else ""
            stadtteil = extract_stadtteil(ort_text + " " + titel)

            price_el = card.select_one(".aditem-main--middle--price-shipping--price, .aditem-main--middle--price")
            kaltmiete = _to_float(price_el.get_text()) if price_el else None

            full_text = card.get_text(" ", strip=True)
            flaeche = _qm(full_text + " " + titel)
            zimmer = _zimmer(titel + " " + full_text)

            return Listing(
                id=f"ka-{adid}",
                portal="kleinanzeigen",
                url=url or _BASE_URL,
                titel=titel[:80],
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=None,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=[f"PLZ:{plz}"] if plz else [],
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _qm(text: str) -> Optional[float]:
    m = re.search(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*m²", text)
    return _to_float(m.group(1)) if m else None


def _zimmer(text: str) -> Optional[float]:
    m = re.search(r"(\d(?:[.,]\d)?)\s*[-\s]?Zimmer", text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[^\d,.]", "", s).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None
