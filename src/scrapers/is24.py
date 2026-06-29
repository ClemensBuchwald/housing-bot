"""ImmobilienScout24 — über die offizielle Mobile-App-API.

Technischer Befund (Live-Analyse 2026-06-06):
  - HTML-Website ist 401-bot-geblockt.
  - Die Mobile-API liefert vollständige Listings ohne Block:
      GET https://api.mobile.immobilienscout24.de/search
          ?searchType=region&realEstateType=apartmentRent
          &geocodes=<CW-Ortsteile>&pageSize=N&pageNumber=P
      Header: User-Agent: ImmoScout24_2410_28_._
  - CW-Geocodes (Ortsteil-Ebene):
      1276003001011 = Charlottenburg
      1276003001020 = Wilmersdorf (inkl. Grunewald, Halensee, Schmargendorf)
    → zusammen = Bezirk Charlottenburg-Wilmersdorf (~519 Wohnungen, 18 Seiten)
  - Pro Eintrag: id, title, address.line (Straße/PLZ/Ortsteil), attributes
    (Preis/m²/Zimmer), titlePicture, isPrivate
  - Detail-URL: https://www.immobilienscout24.de/expose/{id}

Geografischer Filter: geo.py (Ortsteil + PLZ in address.line)
Nur Miete (apartmentRent) — Kauf ausgeschlossen.
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

_API = "https://api.mobile.immobilienscout24.de/search"
_UA = "ImmoScout24_2410_28_._"
_GEOCODES = "1276003001011,1276003001020"  # Charlottenburg + Wilmersdorf(/Grunewald/Halensee)
_EXPOSE = "https://www.immobilienscout24.de/expose/{}"
_MAX_PAGES = 6  # ~120 neueste; Dedup fängt Wiederholungen ab


class IS24Scraper(BaseScraper):
    name = "is24"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        all_listings: List[Listing] = []
        page = 1
        max_pages = _MAX_PAGES

        while page <= max_pages:
            resp = self.get(
                _API,
                params={
                    "searchType": "region",
                    "realEstateType": "apartmentRent",
                    "geocodes": _GEOCODES,
                    "pageSize": 25,
                    "pageNumber": page,
                    "sorting": "-firstactivation",  # neueste zuerst
                },
                headers={"User-Agent": _UA, "Accept": "application/json"},
                min_delay=1.0, max_delay=2.5,
            )
            if resp is None:
                break
            try:
                data = resp.json()
            except Exception:
                logger.warning("[%s] Antwort ist kein JSON", self.name)
                break

            if page == 1:
                total_pages = data.get("numberOfPages", 1)
                max_pages = min(_MAX_PAGES, total_pages)
                logger.info("[%s] %s CW-Wohnungen, hole %d Seiten",
                            self.name, data.get("totalResults"), max_pages)

            results = data.get("results", [])
            if not results:
                break
            for item in results:
                listing = self._parse(item)
                if listing and in_zielgebiet(listing):
                    all_listings.append(listing)
            page += 1

        logger.info("[%s] %d Inserate im Zielgebiet CW", self.name, len(all_listings))
        return all_listings

    def _parse(self, item: dict) -> Optional[Listing]:
        try:
            rid = str(item.get("id", ""))
            if not rid:
                return None
            titel = item.get("title", "Wohnung")
            addr = (item.get("address") or {}).get("line", "")
            plz_m = re.search(r"\b(1[0-4]\d{3})\b", addr)
            plz = plz_m.group(1) if plz_m else ""
            stadtteil = extract_stadtteil(addr)

            kalt = warm = flaeche = zimmer = None
            for a in item.get("attributes", []):
                v = a.get("value", "")
                if "€" in v and kalt is None:
                    kalt = _num(v)
                elif "m²" in v and flaeche is None:
                    flaeche = _num(v)
                elif "Zi" in v and zimmer is None:
                    zimmer = _num(v)

            merkmale = []
            if plz:
                merkmale.append(f"PLZ:{plz}")
            if addr:
                merkmale.append(f"Adresse:{addr}")
            if item.get("isPrivate"):
                merkmale.append("Privatangebot")

            return Listing(
                id=f"is24-{rid}",
                portal="is24",
                url=_EXPOSE.format(rid),
                titel=titel[:90],
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=kalt,
                warmmiete=warm,
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=merkmale,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _num(s: str) -> Optional[float]:
    # "1.185 €" / "42 m²" / "2,5 Zi." → float
    s = s.replace("\xa0", " ")
    m = re.search(r"([\d.]+(?:,\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None
