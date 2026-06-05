"""Heimstaden — großer privater Bestandshalter (JS-gerendert).

Erste JS-Quelle auf Basis von BrowserScraper (Playwright/Chromium).

Befund (Phase 4): Listings werden über ein JS-Widget nachgeladen, kein
serverseitiger Feed. Daher Headless-Browser.

Da die gerenderten Selektoren erst im Browser sichtbar sind, arbeitet dieser
Scraper diagnose-first: Findet er mit den Kandidaten-Selektoren nichts, loggt
er die tatsächliche Kartenstruktur (diagnostic_dump) — damit lassen sich die
Selektoren über die Server-Logs in einem zweiten Schritt fixieren.

Geografischer Filter: geo.py
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from src.models import Listing
from src.scrapers.browser_base import BrowserScraper
from src.scrapers.geo import in_zielgebiet, extract_stadtteil

logger = logging.getLogger(__name__)

_BASE_URL = "https://heimstaden.com"

# Kandidaten-Selektoren (werden bei Fehlschlag durch Diagnose ersetzt)
_CARD_SELECTORS = [
    "[class*=property-card]",
    "[class*=object-card]",
    "[class*=immo-card]",
    "[class*=search-result]",
    "article[class*=card]",
    "a[href*='/immobiliensuche/'][class*=card]",
]


class HeimstadenScraper(BrowserScraper):
    name = "heimstaden"
    wait_selector = "[class*=card], article, [class*=result]"

    def start_urls(self) -> List[str]:
        return ["https://heimstaden.com/de/immobiliensuche/wohnung-berlin/"]

    def parse(self, page_html: str, url: str) -> List[Listing]:
        soup = self.parse_html(page_html)

        cards = []
        for sel in _CARD_SELECTORS:
            cards = soup.select(sel)
            if cards:
                logger.info("[%s] Selektor '%s' → %d Karten", self.name, sel, len(cards))
                break

        if not cards:
            logger.warning("[%s] Keine Karten mit Kandidaten-Selektoren — Diagnose folgt:", self.name)
            self.diagnostic_dump(soup)
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing and in_zielgebiet(listing):
                listings.append(listing)
        return listings

    def parse_html(self, html: str):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card if card.name == "a" else card.select_one("a[href]")
            href = link.get("href", "") if link else ""
            url = (_BASE_URL + href) if href.startswith("/") else href
            listing_id = re.sub(r"\W+", "-", href.split("?")[0].rstrip("/").split("/")[-1])[:60] or "x"

            text = card.get_text(" ", strip=True)
            stadtteil = extract_stadtteil(text)
            plz_m = re.search(r"\b(1[0-4]\d{3})\b", text)
            plz = plz_m.group(1) if plz_m else ""

            kaltmiete = _eur(text, "Kaltmiete") or _first_eur(text)
            warmmiete = _eur(text, "Warmmiete")
            flaeche = _qm(text)
            zimmer = _zimmer(text)

            return Listing(
                id=f"heimstaden-{listing_id}",
                portal="heimstaden",
                url=url or _BASE_URL,
                titel=(text[:60] or "Heimstaden Wohnung"),
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=warmmiete,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=[f"PLZ:{plz}"] if plz else [],
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _eur(text: str, label: str) -> Optional[float]:
    m = re.search(rf"{label}\s*([\d.]+(?:,\d{{2}})?)\s*€|([\d.]+(?:,\d{{2}})?)\s*€\s*{label}", text, re.IGNORECASE)
    return _to_float(m.group(1) or m.group(2)) if m else None


def _first_eur(text: str) -> Optional[float]:
    m = re.search(r"([\d.]+(?:,\d{2})?)\s*€", text)
    return _to_float(m.group(1)) if m else None


def _qm(text: str) -> Optional[float]:
    m = re.search(r"([\d,]+)\s*m²", text)
    return _to_float(m.group(1)) if m else None


def _zimmer(text: str) -> Optional[float]:
    m = re.search(r"(\d(?:,\d)?)\s*Zimmer", text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
