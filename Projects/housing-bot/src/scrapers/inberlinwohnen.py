"""inberlinwohnen.de — Aggregationsportal der 6 landeseigenen Wohnungsunternehmen.

Abgedeckt: degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM

Technischer Befund (Live-Analyse 2026-06-05):
  - Server-seitig gerendert, kein AJAX nötig
  - Listings: div[class*='results__row'] — je 2 Rows pro Inserat:
      Row 1 (gerade): Adresse | Zimmer | Fläche | Kaltmiete | Nebenkosten | Gesamtmiete | Datum
      Row 2 (ungerade): ausgeklappter Detail-Bereich mit externem "Alle Details"-Link
  - Externe Links zeigen direkt auf Provider-Seiten (degewo.de, wbm.de, etc.)
  - Bezirksfilter: ?bezirk=charlottenburg-wilmersdorf (serverseitig, aber aktuell 0 CW-Angebote)
  - Pagination: ?paged=N (wenn mehr als 20 Listings)

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

_BASE_URL = "https://www.inberlinwohnen.de"
_SEARCH_URL = "https://www.inberlinwohnen.de/wohnungsfinder/"
_PROVIDER_DOMAINS = ["degewo", "gesobau", "gewobag", "howoge", "stadtundland", "wbm.de"]


class InBerlinWohnenScraper(BaseScraper):
    name = "inberlinwohnen"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        all_listings: List[Listing] = []
        page = 1
        max_pages = 3  # Fallback bis Gesamtzahl bekannt

        while page <= max_pages:
            # Kein bezirk-Parameter — Filter ist clientseitig (JS), hat keine Wirkung
            # Kürzere Pause für inberlinwohnen (29 Seiten × Pause = Gesamtlaufzeit)
            resp = self.get(_SEARCH_URL, params={"paged": page}, min_delay=1.0, max_delay=2.5)
            if resp is None:
                break

            soup = self.parse(resp.text)

            # Gesamtzahl auf Seite 1 auslesen → max_pages berechnen
            if page == 1:
                import math, re as _re
                m = _re.search(r"von\s+(\d+)\s+Angeboten?", soup.get_text())
                if m:
                    total = int(m.group(1))
                    max_pages = math.ceil(total / 10)
                    logger.info("[%s] Gesamt %d Angebote → %d Seiten", self.name, total, max_pages)
                else:
                    max_pages = 30  # Fallback

            # Alle results__row-Divs
            all_rows = soup.find_all("div", class_=lambda c: c and "results__row" in c)
            if not all_rows:
                if page == 1:
                    logger.warning("[%s] Keine results__row-Elemente gefunden — Selektor prüfen.", self.name)
                break

            # Externe Anbieter-Links
            all_ext_links = [
                a["href"]
                for a in soup.find_all("a", href=True)
                if any(d in a["href"] for d in _PROVIDER_DOMAINS)
            ]

            # Daten-Rows (mit Adresse/Zimmer) vs. Detail-Panels
            data_rows = [r for r in all_rows if "Zimmeranzahl" in r.get_text() or "Adresse" in r.get_text()]
            detail_rows = [r for r in all_rows if "WBS" in r.get_text()]

            logger.debug("[%s] Seite %d/%d: %d Listings", self.name, page, max_pages, len(data_rows))

            for idx, row in enumerate(data_rows):
                ext_link = all_ext_links[idx] if idx < len(all_ext_links) else _SEARCH_URL
                detail = detail_rows[idx] if idx < len(detail_rows) else None
                listing = self._parse_row(row, ext_link, detail)
                if listing and in_zielgebiet(listing):
                    all_listings.append(listing)

            page += 1

        logger.info("[%s] %d Inserate im Zielgebiet CW (von %d Seiten)", self.name, len(all_listings), page - 1)
        return all_listings

    def _parse_row(self, row, ext_link: str, detail=None) -> Optional[Listing]:
        try:
            text = row.get_text(" | ", strip=True)

            # Adresse (enthält PLZ)
            addr = _extract_field(text, "Adresse")
            plz_match = re.search(r"\b(\d{5})\b", addr or "")
            plz = plz_match.group(1) if plz_match else ""

            # Ortsteil aus Adresse extrahieren (kommt nach der PLZ)
            stadtteil = None
            if addr:
                stadtteil = extract_stadtteil(addr)

            # Preise
            kaltmiete = _to_float(_extract_field(text, "Kaltmiete"))
            nebenkosten = _to_float(_extract_field(text, "Nebenkosten"))
            warmmiete = _to_float(_extract_field(text, "Gesamtmiete"))

            # Fläche und Zimmer
            zimmer_raw = _extract_field(text, "Zimmeranzahl")
            zimmer = _to_float(zimmer_raw)
            flaeche_raw = _extract_field(text, "Wohnfläche")
            flaeche = _to_float(flaeche_raw.replace(" m²", "") if flaeche_raw else None)

            # Einzugsdatum
            einzug_str = _extract_field(text, "Bezugsfertig ab")

            # Anbieter aus dem externen Link
            anbieter = _extract_anbieter(ext_link)

            # ID aus PLZ + Zimmer + Kaltmiete (stabil genug für Dedup)
            id_raw = f"{anbieter}-{plz}-{zimmer_raw}-{kaltmiete}"
            listing_id = re.sub(r"[^a-zA-Z0-9]", "-", id_raw)[:80]

            titel = f"[{anbieter}] {zimmer_raw} Zi, {flaeche_raw} — {addr}" if addr else f"[{anbieter}] Wohnung"

            return Listing(
                id=f"ibw-{listing_id}",
                portal="inberlinwohnen",
                url=ext_link,
                titel=titel,
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=warmmiete,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=_build_merkmale(plz, einzug_str, detail),
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _extract_field(text: str, label: str) -> Optional[str]:
    """Extrahiert den Wert nach einem Label aus dem pipe-getrennten Row-Text."""
    m = re.search(rf"{re.escape(label)}:\s*\|?\s*([^|]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _build_merkmale(plz: str, einzug: str, detail) -> List[str]:
    m = []
    if plz:
        m.append(f"PLZ:{plz}")
    if einzug:
        m.append(f"Einzug:{einzug}")
    if detail:
        t = detail.get_text(" ", strip=True)
        for label in ["WBS", "Etage", "Heizung"]:
            v = _extract_field(t, label)
            if v:
                m.append(f"{label}:{v}")
    return m


def _extract_anbieter(url: str) -> str:
    mapping = {
        "degewo": "degewo", "gesobau": "GESOBAU", "gewobag": "Gewobag",
        "howoge": "HOWOGE", "stadtundland": "STADT UND LAND", "wbm": "WBM",
    }
    for key, name in mapping.items():
        if key in url.lower():
            return name
    return "landeseigen"


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        cleaned = re.sub(r"[^\d,.]", "", s).replace(".", "").replace(",", ".")
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None
