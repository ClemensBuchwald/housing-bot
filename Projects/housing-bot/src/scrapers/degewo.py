"""degewo — landeseigene Wohnungsbaugesellschaft.

Technischer Befund (Live-Analyse 2026-06-05, KORRIGIERT):
  - URL: https://immosuche.degewo.de/immosuche
  - ENTGEGEN früherer Annahme SERVER-SEITIG gerendert (TYPO3 + tx_openimmo)
  - KEIN Playwright nötig.
  - Container: div.c-teaser--apartment (10/Seite, ~68 gesamt citywide)
  - Felder im Teaser-Text: Titel (enthält Ortsteil), Straße, Warmmiete, Zimmer, m²
  - Detail-Link: /immosuche/details/{slug}
  - Pagination: cHash-Token (TYPO3 anti-tamper) — nur die im HTML vorhandenen
    Seiten-Links sind nutzbar. Bezirksfilter via Formular braucht JS/cHash,
    daher: alle erreichbaren Seiten holen und per geo.py auf CW filtern.

Limitierung: ohne gültigen cHash sind nicht zwingend alle Seiten erreichbar.
Für den CW-Fokus unkritisch (degewo hat dort meist wenige/keine Angebote),
Quelle bleibt pollbar (Regel: 0 Treffer ≠ wertlos).

Geografischer Filter: geo.py (Ortsteil im Titel/Straße)
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime
from typing import List, Optional, Set

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper
from src.scrapers.geo import in_zielgebiet, extract_stadtteil

logger = logging.getLogger(__name__)

_BASE_URL = "https://immosuche.degewo.de"
_SEARCH_URL = "https://immosuche.degewo.de/immosuche"
_NON_FLAT = re.compile(r"stellplatz|garage|gewerbe|ladenlokal|büro", re.IGNORECASE)


class DegewoScraper(BaseScraper):
    name = "degewo"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL, min_delay=1.5, max_delay=3.0)
        if resp is None:
            return []

        page_html = resp.text
        all_cards = []
        seen_urls: Set[str] = set()

        # Seite 1
        all_cards += self._cards_from(page_html)

        # Weitere Seiten über vorhandene cHash-Pagination-Links
        for page_url in self._pagination_links(page_html):
            r = self.get(page_url, min_delay=1.5, max_delay=3.0)
            if r is None:
                continue
            all_cards += self._cards_from(r.text)

        listings = []
        for card in all_cards:
            listing = self._parse_card(card)
            if listing is None:
                continue
            if listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            if in_zielgebiet(listing):
                listings.append(listing)

        logger.info("[%s] %d Inserate im Zielgebiet CW (von %d Karten)",
                    self.name, len(listings), len(all_cards))
        return listings

    def _cards_from(self, page_html: str):
        return self.parse(page_html).select("div.c-teaser--apartment")

    def _pagination_links(self, page_html: str) -> List[str]:
        raw = page_html.replace("&#x3D;", "=").replace("&amp;", "&")
        paths = set(re.findall(r"(/immosuche\?tx_openimmo_immobilie%5Bpage%5D=\d+[^\"#]*)", raw))
        return [_BASE_URL + _html.unescape(p) for p in sorted(paths)]

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card.select_one("a[href*='/details/']")
            href = link["href"] if link else ""
            url = (_BASE_URL + href) if href.startswith("/") else href
            slug = href.rstrip("/").split("/")[-1] if href else ""

            title_el = card.select_one("h2, h3, .c-teaser__title")
            titel = title_el.get_text(strip=True) if title_el else card.get_text(" ", strip=True)[:60]

            if _NON_FLAT.search(titel):
                return None

            text = card.get_text(" ", strip=True)
            stadtteil = extract_stadtteil(titel + " " + text)

            warmmiete = _eur(text, "Warmmiete") or _first_eur(text)
            kaltmiete = _eur(text, "Kaltmiete") or _eur(text, "Nettokaltmiete")
            flaeche = _qm(text)
            zimmer = _zimmer(text)

            return Listing(
                id=f"degewo-{slug or re.sub(r'.W', '', titel)[:40]}",
                portal="degewo",
                url=url or _SEARCH_URL,
                titel=titel[:80],
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


def _eur(text: str, label: str) -> Optional[float]:
    m = re.search(rf"([\d.]+,\d{{2}})\s*€\s*{label}|{label}\s*([\d.]+,\d{{2}})\s*€", text, re.IGNORECASE)
    if m:
        return _to_float(m.group(1) or m.group(2))
    return None


def _first_eur(text: str) -> Optional[float]:
    m = re.search(r"([\d.]+,\d{2})\s*€", text)
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
