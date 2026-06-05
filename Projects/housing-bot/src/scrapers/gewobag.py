"""Gewobag — landeseigene Wohnungsbaugesellschaft.

Technischer Befund (Live-Analyse 2026-06-05):
  - URL: https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/?bezirke[]=charlottenburg-wilmersdorf
  - Server-seitig gerendert, 200 OK ohne Login
  - Container: article.angebot-big-box.gw-offer
  - Adresse: .angebot-address → "Adresse Reichweindamm 42, 13627 Berlin/Charlottenburg-Wilmersdorf"
  - Gesamtmiete: "Gesamtmiete ab 90,00€"
  - Detail-Link: a "Mietangebot ansehen" → /fuer-mietinteressentinnen/mietangebote/{id}/
  - WICHTIG: Liste enthält auch Stellplätze/Garagen/Gewerbe — werden gefiltert.

Geografischer Filter: geo.py (PLZ + Bezirk in Adresse)
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

_BASE_URL = "https://www.gewobag.de"
_SEARCH_URL = (
    "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/"
    "?bezirke%5B%5D=charlottenburg-wilmersdorf"
)

# Karten, die KEINE Wohnung sind (Titel-Schlüsselwörter)
_NON_FLAT = re.compile(
    r"stellplatz|garage|tiefgarage|gewerbe|ladenlokal|büro|lager|"
    r"praxis|stellfläche|pkw|parkpl",
    re.IGNORECASE,
)


class GewobagScraper(BaseScraper):
    name = "gewobag"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL, min_delay=2.0, max_delay=4.0)
        if resp is None:
            return []

        soup = self.parse(resp.text)
        cards = soup.select("article.angebot-big-box.gw-offer")
        if not cards:
            logger.warning("[%s] Keine Karten gefunden — Selektor prüfen.", self.name)
            return []

        listings = []
        skipped_non_flat = 0
        for card in cards:
            listing = self._parse_card(card)
            if listing is None:
                skipped_non_flat += 1
                continue
            if in_zielgebiet(listing):
                listings.append(listing)

        logger.info(
            "[%s] %d Wohnungen im Zielgebiet (%d Stellplätze/Gewerbe gefiltert)",
            self.name, len(listings), skipped_non_flat,
        )
        return listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            title_el = card.select_one("h3, h2")
            titel = title_el.get_text(strip=True) if title_el else ""

            # Stellplätze/Gewerbe aussortieren
            if _NON_FLAT.search(titel):
                return None

            # Detail-Link ("Mietangebot ansehen", kein Bild-Upload)
            url = ""
            for a in card.select("a[href]"):
                href = a.get("href", "")
                if "uploads" not in href and "/mietangebote/" in href:
                    url = href if href.startswith("http") else _BASE_URL + href
                    break
            listing_id = url.rstrip("/").split("/")[-1] if url else re.sub(r"\W", "", titel[:30])

            # Adresse → Straße, PLZ, Bezirk
            addr_el = card.select_one(".angebot-address")
            addr_text = addr_el.get_text(" ", strip=True) if addr_el else card.get_text(" ", strip=True)
            plz_m = re.search(r"\b(\d{5})\b", addr_text)
            plz = plz_m.group(1) if plz_m else ""
            stadtteil = extract_stadtteil(addr_text)

            card_text = card.get_text(" ", strip=True)
            warmmiete = _miete(card_text, "Gesamtmiete")
            kaltmiete = _miete(card_text, "Nettokaltmiete") or _miete(card_text, "Kaltmiete")
            flaeche = _qm(card_text)
            zimmer = _zimmer(card_text)

            # Ohne Zimmerangabe UND ohne Fläche: vermutlich kein Wohnungsinserat
            if zimmer is None and flaeche is None:
                return None

            return Listing(
                id=f"gewobag-{listing_id}",
                portal="gewobag",
                url=url or _SEARCH_URL,
                titel=titel or "Gewobag Wohnung",
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


def _miete(text: str, label: str) -> Optional[float]:
    m = re.search(rf"{label}\s*(?:ab\s*)?([\d.]+,\d{{2}})\s*€", text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _qm(text: str) -> Optional[float]:
    m = re.search(r"Fläche\s*([\d.,]+)\s*m²|([\d.,]+)\s*m²", text)
    g = m.group(1) or m.group(2) if m else None
    return _to_float(g) if g else None


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
