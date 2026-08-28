"""Charlottenburger Baugenossenschaft eG ("CHARLOTTE") — charlotte1907.de

Die einzige geprüfte Genossenschaft mit echtem Bestand im Zielgebiet UND einem
maschinell lesbaren Angebotsfeed. Kein Duplikat: Genossenschaftswohnungen laufen
nicht über IS24/Immowelt/inberlinwohnen.

Technischer Befund (Live-Analyse 2026-08):
  URL: https://charlotte1907.de/wohnungsangebote/woechentliche-angebote
  TYPO3 + tx_immobilie, vollständig server-gerendert (kein JS nötig).
  robots.txt erlaubt es (verbietet nur /typo3conf/).

  Container: div.immobilie-list  (aktuell 10 Angebote, keine Pagination)
  Felder:
    h2                  → Bezirk  ("Spandau", "Charlottenburg - Wilmersdorf")
    span.strt/.houseNo  → Straße + Hausnummer
    span.postcode       → PLZ
    div.item > b        → Label/Wert-Paare: Etage, Zimmer, Wohnfläche,
                          Ausstattung, Gesamtmiete in Euro, Voraus. frei ab

  Hinweis: Der GET-Parameter tx_immobilie_housing[place] filtert serverseitig
  NICHT — daher die (kurze) Gesamtliste holen und selbst über geo.py filtern.

  Aktualisierung: wöchentlich (montags). Ein Poll pro Zyklus ist unkritisch.

Wichtig für die Erwartung: Die Angebote sind mit "nur für Mitglieder" markiert —
die Bewerbung setzt eine Mitgliedschaft voraus. Die Inserate selbst sind öffentlich.
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

_BASE_URL = "https://charlotte1907.de"
_SEARCH_URL = "https://charlotte1907.de/wohnungsangebote/woechentliche-angebote"


class CharlotteScraper(BaseScraper):
    name = "charlotte"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        resp = self.get(_SEARCH_URL, min_delay=1.0, max_delay=2.5)
        if resp is None:
            return []

        soup = self.parse(resp.text)
        cards = soup.select("div.immobilie-list")
        if not cards:
            logger.warning("[%s] Keine div.immobilie-list gefunden — Selektor prüfen.", self.name)
            return []

        listings = []
        for card in cards:
            listing = self._parse_card(card)
            if listing and in_zielgebiet(listing):
                listings.append(listing)

        logger.info("[%s] %d Inserate im Zielgebiet CW (von %d Angeboten)",
                    self.name, len(listings), len(cards))
        return listings

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            # Wohnungs-Nr. als stabile ID
            kopf = card.select_one("h6")
            kopf_text = kopf.get_text(" ", strip=True) if kopf else ""
            m_id = re.search(r"WOHNUNGS-Nr\.\s*(\S+)", kopf_text)
            listing_id = re.sub(r"\W+", "-", m_id.group(1)) if m_id else re.sub(r"\W+", "-", kopf_text)[:40]

            bez_el = card.select_one("h2")
            bezirk = bez_el.get_text(" ", strip=True) if bez_el else ""

            strasse = _txt(card, "span.strt")
            hausnr = _txt(card, "span.houseNo")
            plz = _txt(card, "span.postcode")
            adresse = " ".join(x for x in [strasse, hausnr] if x).strip()

            felder = _felder(card)
            zimmer = _num(felder.get("Zimmer"))
            flaeche = _num(felder.get("Wohnfläche"))
            warmmiete = _num(felder.get("Gesamtmiete in Euro"))
            etage = felder.get("Etage") or ""
            ausstattung = felder.get("Ausstattung") or ""
            frei_ab = felder.get("Voraus. frei ab") or ""

            # Ortsteil: Bezirksangabe kann "Charlottenburg - Wilmersdorf" lauten —
            # extract_stadtteil maskiert Bezirksnamen, daher Adresse mitgeben.
            stadtteil = extract_stadtteil(f"{bezirk} {adresse}") or extract_stadtteil(bezirk)

            merkmale = []
            if plz:
                merkmale.append(f"PLZ:{plz}")
            if adresse:
                merkmale.append(f"Adresse:{adresse}, {plz} Berlin")
            if etage:
                merkmale.append(f"Etage:{etage}")
            if ausstattung:
                merkmale.append(f"Ausstattung:{ausstattung}")
            if frei_ab:
                merkmale.append(f"Frei ab:{frei_ab}")
            merkmale.append("Genossenschaft — Mitgliedschaft erforderlich")

            zi_txt = f"{zimmer:g} Zi" if zimmer else "? Zi"
            fl_txt = f"{flaeche:g} m²" if flaeche else "? m²"
            titel = f"[CHARLOTTE] {zi_txt} · {fl_txt} · {adresse or bezirk}"

            return Listing(
                id=f"charlotte-{listing_id}",
                portal="charlotte",
                url=_SEARCH_URL,   # Einzelinserate haben keine eigene URL
                titel=titel[:90],
                stadt="Berlin",
                stadtteil=stadtteil,
                kaltmiete=None,
                warmmiete=warmmiete,   # Gesamtmiete = warm
                flaeche=flaeche,
                zimmer=zimmer,
                merkmale=merkmale,
                gefunden_am=datetime.now(),
            )
        except Exception as e:
            logger.debug("[%s] Parse-Fehler: %s", self.name, e)
            return None


def _txt(card, sel: str) -> str:
    el = card.select_one(sel)
    return el.get_text(" ", strip=True) if el else ""


def _felder(card) -> dict:
    """Label/Wert-Paare aus den div.item-Blöcken ('<b>Etage</b>: 2. OG')."""
    out = {}
    for item in card.select("div.item"):
        b = item.find("b")
        if not b:
            continue
        label = b.get_text(" ", strip=True).rstrip(":").strip()
        wert = item.get_text(" ", strip=True)
        wert = wert[len(b.get_text(" ", strip=True)):].lstrip(": ").strip()
        if label:
            out[label] = wert
    return out


def _num(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"[\d.]+(?:,\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None
