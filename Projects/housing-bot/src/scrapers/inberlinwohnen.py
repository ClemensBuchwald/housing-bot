"""inberlinwohnen.de — Aggregationsportal der 6 landeseigenen Wohnungsunternehmen.

Abgedeckt: degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM

Technischer Ansatz:
  Die Seite lädt Inserate per AJAX-Request gegen eine interne JSON-API.
  Endpunkt: POST https://inberlinwohnen.de/wp-json/inberlinwohnen/v1/wohnungsfinder
  (ermittelt per Browser-DevTools Network-Tab → XHR-Requests beim Laden des Finders)

  Falls der API-Endpunkt sich ändert: HTML-Fallback über CSS-Selektoren.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# Bekannte API-Endpunkte (zu verifizieren mit DevTools → Network → XHR)
_API_URL = "https://inberlinwohnen.de/wp-json/inberlinwohnen/v1/wohnungsfinder"
_SEARCH_URL = "https://inberlinwohnen.de/wohnungsfinder/"


class InBerlinWohnenScraper(BaseScraper):
    name = "inberlinwohnen"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        listings: List[Listing] = []

        # Versuch 1: JSON-API
        result = self._fetch_api(criteria)
        if result:
            return result

        # Versuch 2: HTML-Fallback
        logger.info("[%s] API nicht verfügbar, versuche HTML-Fallback", self.name)
        return self._fetch_html(criteria)

    def _fetch_api(self, criteria: Criteria) -> List[Listing]:
        """Versucht die interne WP-REST-API abzufragen."""
        payload = {
            "bezirke": [],       # leer = alle Bezirke
            "zimmer_von": str(int(criteria.zimmer.min)) if criteria.zimmer.min else "",
            "zimmer_bis": str(int(criteria.zimmer.max)) if criteria.zimmer.max else "",
            "miete_von": "",
            "miete_bis": str(int(criteria.preis.warmmiete_max)) if criteria.preis.warmmiete_max else "",
            "flaeche_von": str(int(criteria.flaeche.min_qm)) if criteria.flaeche.min_qm else "",
            "flaeche_bis": "",
        }

        resp = self.get(
            _API_URL,
            headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

        if resp is None:
            return []

        try:
            data = resp.json()
        except Exception:
            logger.warning("[%s] API-Antwort ist kein JSON", self.name)
            return []

        # Struktur variiert — defensiv parsen
        items = data if isinstance(data, list) else data.get("wohnungen", data.get("results", []))
        if not items:
            logger.info("[%s] API liefert 0 Inserate", self.name)
            return []

        listings = []
        for item in items:
            listing = self._parse_api_item(item)
            if listing:
                listings.append(listing)

        logger.info("[%s] %d Inserate via API", self.name, len(listings))
        return listings

    def _parse_api_item(self, item: dict) -> Listing | None:
        try:
            listing_id = str(item.get("id") or item.get("ID") or item.get("expose_id", ""))
            if not listing_id:
                return None

            # Feldnamen variieren je nach API-Version
            titel = item.get("title") or item.get("post_title") or item.get("bezeichnung", "Unbekannt")
            url = item.get("url") or item.get("link") or item.get("permalink", _SEARCH_URL)

            warmmiete = self._to_float(item.get("warmmiete") or item.get("gesamtmiete"))
            kaltmiete = self._to_float(item.get("kaltmiete") or item.get("nettokaltmiete"))
            flaeche = self._to_float(item.get("flaeche") or item.get("wohnflaeche"))
            zimmer = self._to_float(item.get("zimmer") or item.get("zimmeranzahl"))
            stadtteil = item.get("bezirk") or item.get("stadtteil") or item.get("ortsteil")
            anbieter = item.get("anbieter") or item.get("gesellschaft", "")

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
            logger.debug("[%s] Parse-Fehler: %s — %s", self.name, e, item)
            return None

    def _fetch_html(self, criteria: Criteria) -> List[Listing]:
        """HTML-Fallback: parst die Wohnungsfinder-Seite direkt."""
        resp = self.get(_SEARCH_URL)
        if resp is None:
            return []

        soup = self.parse(resp.text)
        listings = []

        # Selektoren müssen nach Live-Inspektion ggf. angepasst werden
        # Typische Struktur: .wohnungsfinder-item oder article.wohnung
        cards = (
            soup.select(".wohnungsfinder-item")
            or soup.select("article.wohnung")
            or soup.select(".listing-item")
        )

        if not cards:
            logger.warning(
                "[%s] Keine Inserate im HTML gefunden. "
                "Seite wahrscheinlich JS-gerendert — Playwright nötig.",
                self.name,
            )
            return []

        for card in cards:
            listing = self._parse_html_card(card)
            if listing:
                listings.append(listing)

        logger.info("[%s] %d Inserate via HTML-Fallback", self.name, len(listings))
        return listings

    def _parse_html_card(self, card) -> Listing | None:
        try:
            link = card.select_one("a[href]")
            url = link["href"] if link else _SEARCH_URL
            titel = card.select_one("h2, h3, .title, .bezeichnung")
            titel_text = titel.get_text(strip=True) if titel else "Unbekannt"
            listing_id = url.split("/")[-2] if url != _SEARCH_URL else card.get("data-id", "unknown")
            return Listing(
                id=f"ibw-html-{listing_id}",
                portal="inberlinwohnen",
                url=url,
                titel=titel_text,
                stadt="Berlin",
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] HTML-Parse-Fehler: %s", self.name, e)
            return None

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).replace(".", "").replace(",", ".").replace("€", "").strip())
        except (ValueError, TypeError):
            return None
