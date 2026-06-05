"""inberlinwohnen.de — Aggregationsportal der 6 landeseigenen Wohnungsunternehmen.

Abgedeckt: degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM

Technischer Befund (Live-Prüfung 2026-06-05):
  - Server-seitig gerendert, kein AJAX nötig
  - Pagination: 10 Angebote pro Seite, 267 Angebote gesamt
  - Bezirksfilter "Charlottenburg-Wilmersdorf" im Formular vorhanden
  - Detailseiten-Links zeigen auf externe Domains (degewo.de, howoge.de etc.)

Geografischer Filter:
  Nur Listings übernehmen, die mindestens einem der Zielorte zugeordnet werden können:
  Charlottenburg, Wilmersdorf, Halensee, Grunewald
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlencode

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper
from src.scrapers.geo import in_zielgebiet, extract_stadtteil

logger = logging.getLogger(__name__)

_BASE_URL = "https://inberlinwohnen.de"
_SEARCH_URL = "https://inberlinwohnen.de/wohnungsfinder/"

# Bezirks-Slug für den Filter (aus der Seite ermittelt)
_BEZIRK_PARAM = "charlottenburg-wilmersdorf"


class InBerlinWohnenScraper(BaseScraper):
    name = "inberlinwohnen"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        listings: List[Listing] = []
        page = 1

        while True:
            params = {
                "bezirk": _BEZIRK_PARAM,
                "paged": page,
            }
            resp = self.get(_SEARCH_URL, params=params)
            if resp is None:
                break

            soup = self.parse(resp.text)
            cards = self._find_cards(soup)

            if not cards:
                if page == 1:
                    logger.warning(
                        "[%s] Keine Karten gefunden. "
                        "Selektoren bitte mit DevTools auf inberlinwohnen.de prüfen.",
                        self.name,
                    )
                break

            for card in cards:
                listing = self._parse_card(card)
                if listing and in_zielgebiet(listing):
                    listings.append(listing)

            # Prüfe ob weitere Seiten existieren
            next_btn = soup.select_one("a.next, .pagination a[rel='next'], a[aria-label='Nächste Seite']")
            if not next_btn:
                break
            page += 1
            if page > 30:  # Sicherheitsgrenze
                break

        logger.info("[%s] %d Inserate im Zielgebiet", self.name, len(listings))
        return listings

    def _find_cards(self, soup):
        """Probiert bekannte Selektoren, gibt die erste funktionierende Liste zurück."""
        selectors = [
            ".wohnungsfinder-item",
            "article.wohnung",
            ".wf-item",
            ".immo-item",
            "li.wohnungsangebot",
            "div[class*='wohnung']",
        ]
        for sel in selectors:
            cards = soup.select(sel)
            if cards:
                logger.debug("[%s] Selektor '%s' → %d Karten", self.name, sel, len(cards))
                return cards
        return []

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            # Link zur Detailseite
            link = card.select_one("a[href]")
            url = link["href"] if link else _SEARCH_URL
            if url.startswith("/"):
                url = _BASE_URL + url

            # ID aus URL ableiten
            listing_id = re.sub(r"[^a-z0-9]", "-", url.lower().split("//")[-1])[:80]

            # Titel
            titel_el = card.select_one("h2, h3, .title, .bezeichnung, [class*='title']")
            titel = titel_el.get_text(strip=True) if titel_el else "Wohnung"

            # Adresse / Stadtteil — aus dem gesamten Kartentext
            text = card.get_text(" ", strip=True)
            stadtteil = extract_stadtteil(text + " " + titel)

            # Preis
            kaltmiete = _extract_price(text, ["Kaltmiete", "Nettokaltmiete"])
            warmmiete = _extract_price(text, ["Warmmiete", "Gesamtmiete", "Gesamtmiete:"])
            if warmmiete is None and kaltmiete is None:
                warmmiete = _first_price(text)

            # Fläche und Zimmer
            flaeche = _extract_qm(text)
            zimmer = _extract_zimmer(text)

            # Anbieter (landeseigene)
            anbieter = _extract_anbieter(url)

            return Listing(
                id=f"ibw-{listing_id}",
                portal="inberlinwohnen",
                url=url,
                titel=f"[{anbieter}] {titel}" if anbieter else titel,
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


def _extract_anbieter(url: str) -> str:
    mapping = {
        "degewo": "degewo", "gesobau": "GESOBAU", "gewobag": "Gewobag",
        "howoge": "HOWOGE", "stadtundland": "STADT UND LAND", "wbm": "WBM",
    }
    for key, name in mapping.items():
        if key in url.lower():
            return name
    return ""


def _extract_price(text: str, labels: List[str]) -> Optional[float]:
    for label in labels:
        m = re.search(rf"{re.escape(label)}[:\s]*([0-9.]+(?:,[0-9]{{2}})?)\s*€?", text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _first_price(text: str) -> Optional[float]:
    m = re.search(r"(\d{3,4}(?:[.,]\d{2})?)\s*€", text)
    return _to_float(m.group(1)) if m else None


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
