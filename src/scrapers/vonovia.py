"""Vonovia — JSON-API (inkl. ehemals Deutsche Wohnen, integriert Herbst 2024).

API-Befund (Live-Analyse 2026-06-05):
  Endpunkt: GET https://www.vonovia.de/api/real-estate/list
  Parameter:
    marketing_type=RENT   — nur Mietobjekte
    city=Berlin           — Stadtfilter
    pageSize=100          — max pro Seite (API-Limit unbekannt, 100 funktioniert)
    offset=N              — Pagination

  Response-Felder (je Eintrag):
    wrk_id          — eindeutige Objekt-ID
    titel           — Titel des Inserats
    strasse         — Straße + Hausnummer
    plz             — PLZ (5-stellig)
    ort             — "Berlin OT Charlottenburg" o.ä.
    preis           — Kaltmiete (EUR)
    groesse         — Wohnfläche (m²)
    anzahl_zimmer   — Zimmeranzahl
    slug            — für Detail-URL
    vermarktungsart_miete — "1" = Miete

  Detail-URL: https://www.vonovia.de/de-de/immobiliensuche/detail/{slug}

  Aktuell wenige Berliner Wohnungsangebote (Vonovia-Schwerpunkt außerhalb Berlin),
  aber historisch Bestände in Charlottenburg/Wilmersdorf vorhanden.
  API bleibt als Dauerwächter sinnvoll.

Geografischer Filter: geo.py
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper
from src.scrapers.geo import in_zielgebiet, extract_stadtteil

logger = logging.getLogger(__name__)

_API_URL = "https://www.vonovia.de/api/real-estate/list"
_DETAIL_BASE = "https://www.vonovia.de/de-de/immobiliensuche/detail"
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.vonovia.de/de-de/immobiliensuche",
}


class VonoviaScraper(BaseScraper):
    name = "vonovia"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        all_listings: List[Listing] = []
        offset = 0
        page_size = 100

        while True:
            resp = self.get(
                _API_URL,
                params={
                    "marketing_type": "RENT",
                    "city": "Berlin",
                    "pageSize": page_size,
                    "offset": offset,
                },
                headers=_HEADERS,
            )
            if resp is None:
                break

            try:
                data = resp.json()
            except Exception:
                logger.warning("[%s] API-Antwort ist kein JSON", self.name)
                break

            results = data.get("results", [])
            total = data.get("paging", {}).get("info", {}).get("count", 0)

            # Nur echte Wohnungen (keine Stellplätze/Garagen)
            wohnungen = [
                r for r in results
                if r.get("anzahl_zimmer", 0) > 0 and r.get("groesse", 0) > 10
                and r.get("vermarktungsart_miete") == "1"
            ]

            for item in wohnungen:
                listing = self._parse_item(item)
                if listing and in_zielgebiet(listing):
                    all_listings.append(listing)

            logger.debug(
                "[%s] offset=%d: %d Einträge, %d Wohnungen, gesamt %d",
                self.name, offset, len(results), len(wohnungen), total,
            )

            offset += page_size
            if offset >= total or not results:
                break

        logger.info("[%s] %d Inserate im Zielgebiet CW", self.name, len(all_listings))
        return all_listings

    def _parse_item(self, item: dict) -> Optional[Listing]:
        try:
            wrk_id = str(item.get("wrk_id", ""))
            slug = item.get("slug", wrk_id)
            url = f"{_DETAIL_BASE}/{slug}"

            titel = item.get("titel", "Vonovia Wohnung")
            strasse = item.get("strasse", "")
            plz = item.get("plz", "")
            ort = item.get("ort", "")  # z.B. "Berlin OT Charlottenburg"

            # Stadtteil aus "Berlin OT Charlottenburg" extrahieren
            stadtteil = None
            if "OT " in ort:
                stadtteil_raw = ort.split("OT ")[-1].strip()
                stadtteil = extract_stadtteil(stadtteil_raw) or stadtteil_raw
            else:
                stadtteil = extract_stadtteil(titel + " " + ort)

            kaltmiete = float(item["preis"]) if item.get("preis") else None
            flaeche = float(item["groesse"]) if item.get("groesse") else None
            zimmer = float(item["anzahl_zimmer"]) if item.get("anzahl_zimmer") else None

            merkmale = []
            if plz:
                merkmale.append(f"PLZ:{plz}")
            if strasse:
                merkmale.append(f"Adresse:{strasse} {plz}")

            return Listing(
                id=f"vonovia-{wrk_id}",
                portal="vonovia",
                url=url,
                titel=titel,
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kaltmiete,
                warmmiete=None,  # API liefert nur Kaltmiete
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=merkmale,
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s — %s", self.name, e, item.get("wrk_id"))
            return None
