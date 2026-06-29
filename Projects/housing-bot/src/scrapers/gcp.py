"""Grand City Property (GCP) — großer privater Vermieter mit Berliner Bestand.

JS-Quelle auf Basis von BrowserScraper (Playwright/Chromium).

Befund (Phase 4): /de/wohnungssuche ist server-gerendert (div.real-estate-item),
aber bundesweit; der Ort-Filter ist JS-gesteuert (GET-Filter → 520). Daher Browser:
Ort ins Suchfeld eingeben, dann die gerenderten Karten auslesen.

Reichhaltige Kacheldaten: Adresse, Fläche, Zimmer, Stock (Etage), Balkon, Wanne (Badewanne)
— ideal für die KI-Bewertung. Detail-Link: /wohnungssuche/{stadt}/{strasse}/{id}

Geografischer Filter: geo.py (Ort in Adresse). Selektoren ggf. via Server-Logs nachschärfen.
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

_BASE_URL = "https://www.grandcityproperty.de"


class GCPScraper(BrowserScraper):
    name = "gcp"
    wait_selector = "div.real-estate-item"

    def start_urls(self) -> List[str]:
        return ["https://www.grandcityproperty.de/de/wohnungssuche"]

    def interact(self, page) -> None:
        """Ort 'Berlin' ins Suchfeld eingeben, damit GCP auf Berlin filtert."""
        for sel in ('input[name="search[q]"]', 'input[name="cityField"]',
                    'input[placeholder*="Ort"]', 'input[type="search"]'):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    el.fill("Berlin")
                    page.wait_for_timeout(1200)  # Autocomplete
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2500)   # Ergebnisse laden
                    logger.info("[%s] Ortsfilter 'Berlin' gesetzt via %s", self.name, sel)
                    return
            except Exception:
                continue
        logger.info("[%s] Kein Suchfeld gefunden — parse nationale Liste, geo.py filtert", self.name)

    def parse(self, page_html: str, url: str) -> List[Listing]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page_html, "lxml")
        cards = soup.select("div.real-estate-item")
        if not cards:
            logger.warning("[%s] Keine Karten — Diagnose:", self.name)
            self.diagnostic_dump(soup)
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing and in_zielgebiet(listing):
                listings.append(listing)
        return listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card.select_one('a[href*="/wohnungssuche/"]') or card.select_one("a[href]")
            href = (link.get("href") or "").strip() if link else ""
            url = (_BASE_URL + href) if href.startswith("/") else href
            m_id = re.search(r"/(\d{4}_\d+_\d+_\d+)", href)
            listing_id = m_id.group(1) if m_id else re.sub(r"\W", "", href)[-40:]

            text = card.get_text(" ", strip=True)

            # Adresse: "Strasse 15, Stadt"
            addr_m = re.search(r"([A-ZÄÖÜ][\wäöüß.\- ]+\s\d+[a-z]?,\s*[A-ZÄÖÜ][a-zäöüß]+)", text)
            addr = addr_m.group(1) if addr_m else ""
            stadtteil = extract_stadtteil(addr or text)

            flaeche = _num(text, r"(\d+(?:[.,]\d+)?)\s*m")
            zimmer = _labeled(text, "Zimmer")
            stock = _labeled(text, "Stock")
            preis = _num(text, r"([\d.]+(?:,\d{2})?)\s*€")

            merkmale = []
            if addr:
                merkmale.append(f"Adresse:{addr}")
            if stock is not None:
                merkmale.append("Etage:Erdgeschoss" if stock == 0 else f"Etage:{int(stock)}. OG")
            if re.search(r"\bBalkon\b", text):
                merkmale.append("Balkon")
            if re.search(r"\bWanne\b", text):
                merkmale.append("Badewanne")

            return Listing(
                id=f"gcp-{listing_id}",
                portal="gcp",
                url=url or _BASE_URL,
                titel=(addr or "GCP Wohnung")[:80],
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=preis,
                warmmiete=None,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=merkmale,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _num(text: str, pattern: str) -> Optional[float]:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _labeled(text: str, label: str) -> Optional[float]:
    # "2 | Zimmer" oder "Zimmer 2" oder "4 Stock"
    m = re.search(rf"(\d+(?:[.,]\d+)?)\s*\|?\s*{label}|{label}\s*\|?\s*(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    v = m.group(1) or m.group(2)
    try:
        return float(v.replace(",", "."))
    except (ValueError, TypeError):
        return None
