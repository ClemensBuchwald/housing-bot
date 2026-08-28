"""Immowelt — Wohnungssuche.

Technischer Befund (Live-Analyse 2026-06-05):
  - URL: https://www.immowelt.de/suche/berlin-{ortsteil}/wohnungen/mieten
  - Server-seitig gerendert, 200 OK ohne Login
  - CSS-Klassen sind rotierende Hashes (css-xxxxx) — NICHT nutzbar
  - Stabil: data-testid="cardmfe-container--test-id" (Container)
  - Karten-Text enthält: "1.870 € | Kaltmiete | 2 Zimmer | 71,9 m² | Charlottenburg, Berlin (10623)"
  - Detail-Links: /expose/{id}

Geografischer Filter: geo.py (Ortsteil + PLZ stehen im Kartentext)
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

_BASE_URL = "https://www.immowelt.de"
# Zielortseiten — Immowelt erlaubt Ortsteil-Suche direkt in der URL
_SEARCH_URLS = [
    "https://www.immowelt.de/suche/berlin-charlottenburg/wohnungen/mieten",
    "https://www.immowelt.de/suche/berlin-wilmersdorf/wohnungen/mieten",
    "https://www.immowelt.de/suche/berlin-grunewald/wohnungen/mieten",
    "https://www.immowelt.de/suche/berlin-halensee/wohnungen/mieten",
]


class ImmoweltScraper(BaseScraper):
    name = "immowelt"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        all_listings: List[Listing] = []
        seen_ids = set()

        for url in _SEARCH_URLS:
            resp = self.get(url, min_delay=2.0, max_delay=4.0)
            if resp is None:
                continue
            soup = self.parse(resp.text)
            # Anker = Covering-Link (trägt den /expose/-Link); Daten im Parent-Container
            links = soup.select('a[data-testid="card-mfe-covering-link-testid"]')
            if not links:
                logger.debug("[%s] Keine Covering-Links auf %s", self.name, url)
                continue

            for link in links:
                listing = self._parse_card(link)
                if listing and listing.id not in seen_ids and in_zielgebiet(listing):
                    seen_ids.add(listing.id)
                    all_listings.append(listing)

        logger.info("[%s] %d Inserate im Zielgebiet CW", self.name, len(all_listings))
        return all_listings

    def _parse_card(self, link) -> Optional[Listing]:
        try:
            # link = das Covering-<a> mit /expose/-href; Daten stehen im Parent
            href = link.get("href", "")
            url = (_BASE_URL + href) if href.startswith("/") else href
            m_id = re.search(r"/expose/([a-z0-9-]+)", href)

            container = link.parent or link
            cont = container.select_one('[data-testid="cardmfe-container--test-id"]') or container
            text = cont.get_text(" ", strip=True)

            listing_id = m_id.group(1) if m_id else re.sub(r"\W", "", text[:30])

            # Ort: Format wechselte von "Charlottenburg, Berlin (10623)" zu
            # "{...} {Ortsteil}, {Bezirk} ({PLZ})". Deshalb an der PLZ verankern
            # und den Ortsteil als letzten Großschreib-Block davor nehmen.
            plz = ""
            stadtteil = None
            ort_m = re.search(r"([^|,]+),\s*([^|,()]+?)\s*\((\d{5})\)", text)
            if ort_m:
                plz = ort_m.group(3)
                worte = re.findall(r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)*", ort_m.group(1))
                if worte:
                    stadtteil = worte[-1]
            if not stadtteil:
                stadtteil = extract_stadtteil(text)

            # Preis
            kaltmiete = _price_near(text, "Kaltmiete")
            warmmiete = _price_near(text, "Warmmiete")
            if kaltmiete is None and warmmiete is None:
                kaltmiete = _first_price(text)

            flaeche = _qm(text)
            zimmer = _zimmer(text)

            titel = f"{int(zimmer) if zimmer else '?'} Zi · {flaeche or '?'} m² · {stadtteil or 'Berlin'}"

            # Kein gültiger Expose-Link → Inserat überspringen (keine Basis-URL-Fallbacks)
            if "/expose/" not in url:
                return None

            # Merkmale für die KI-Bewertung anreichern (aus dem Kachel-Text)
            merkmale = []
            if plz:
                merkmale.append(f"PLZ:{plz}")
            etage_m = re.search(r"(\d+)\.\s*(?:Geschoss|OG|Obergeschoss)", text)
            if etage_m:
                merkmale.append(f"Etage:{etage_m.group(1)}. OG")
            elif re.search(r"\bErdgeschoss\b|Hochparterre", text, re.IGNORECASE):
                merkmale.append("Etage:Erdgeschoss/Hochparterre")
            adr_m = re.search(r"([A-ZÄÖÜ][a-zäöüß.\-]+(?:str(?:aße|\.)|damm|allee|platz|weg|ring)\.?\s*\d+[a-z]?)", text)
            if adr_m:
                merkmale.append(f"Adresse:{adr_m.group(1).strip()}")
            beschr_m = re.search(r"Objektbeschreibung\s*(.+)$", text)
            snippet = (beschr_m.group(1) if beschr_m else text)[:350]
            merkmale.append(f"Beschreibung:{snippet}")

            return Listing(
                id=f"immowelt-{listing_id}",
                portal="immowelt",
                url=url,
                titel=titel,
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=warmmiete,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=merkmale,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _price_near(text: str, label: str) -> Optional[float]:
    # "1.870 € Kaltmiete" oder "Kaltmiete 1.870 €"
    for pat in (rf"([\d.]+)\s*€\s*{label}", rf"{label}\s*([\d.]+)\s*€"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _first_price(text: str) -> Optional[float]:
    m = re.search(r"([\d.]{3,6})\s*€", text)
    return _to_float(m.group(1)) if m else None


def _qm(text: str) -> Optional[float]:
    m = re.search(r"(\d{2,3}(?:,\d{1,2})?)\s*m²", text)
    return _to_float(m.group(1)) if m else None


def _zimmer(text: str) -> Optional[float]:
    m = re.search(r"(\d(?:,\d)?)\s*Zimmer", text)
    return _to_float(m.group(1)) if m else None


def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
