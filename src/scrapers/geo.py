"""Geografische Filterlogik für den Housing Bot.

Zielorte: Charlottenburg, Wilmersdorf, Halensee, Grunewald

Ansatz:
  - Texttreffer in Titel, Adresse, Stadtteilfeld
  - PLZ-Prüfung (bekannte PLZ der Zielorte)
  - URL-Slug-Prüfung
  - "Charlottenburg-Wilmersdorf" allein genügt NICHT — zu grob
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.models import Listing

# Harte Zielorte — exakte Treffer geben Punkte
_ZIELORTE = [
    "charlottenburg",
    "wilmersdorf",
    "halensee",
    "grunewald",
]

# PLZ die zu den Zielorten gehören
_ZIEL_PLZ = {
    # Charlottenburg
    "10585", "10587", "10589", "10623", "10625", "10627", "10629",
    "14050", "14052", "14053", "14055", "14057", "14059",
    # Wilmersdorf
    "10707", "10709", "10711", "10713", "10715", "10717", "10719",
    # Halensee
    "10711",  # Halensee liegt in 10711/10713
    # Grunewald
    "14193", "14195",
}

# Diese Begriffe allein reichen NICHT für eine Zuweisung
_ZU_GROB = [
    "charlottenburg-wilmersdorf",
    "berlin",
    "west",
]


def in_zielgebiet(listing: "Listing") -> bool:
    """True wenn das Listing eindeutig einem Zielort zugeordnet werden kann."""
    text = _listing_text(listing)
    return _hat_zielort(text) or _hat_ziel_plz(text)


def extract_stadtteil(text: str) -> Optional[str]:
    """Extrahiert den spezifischsten erkennbaren Stadtteil aus einem Text."""
    text_lower = text.lower()

    # Priorität: Halensee und Grunewald zuerst (werden sonst von Charlottenburg verschluckt)
    for ort in ["halensee", "grunewald", "wilmersdorf", "charlottenburg"]:
        if ort in text_lower:
            return ort.capitalize()

    # Weitere Berliner Stadtteile als Fallback
    weitere = [
        "mitte", "prenzlauer berg", "friedrichshain", "kreuzberg",
        "schöneberg", "tempelhof", "neukölln", "treptow", "pankow",
        "weißensee", "lichtenberg", "marzahn", "hellersdorf",
        "spandau", "reinickendorf", "steglitz", "zehlendorf",
    ]
    for ort in weitere:
        if ort in text_lower:
            return ort.title()

    return None


def _listing_text(listing: "Listing") -> str:
    parts = [
        listing.titel or "",
        listing.stadtteil or "",
        listing.stadt or "",
        listing.url or "",
    ]
    return " ".join(parts).lower()


def _hat_zielort(text: str) -> bool:
    """Prüft auf exakte Ortsteil-Nennung."""
    for ort in _ZIELORTE:
        if ort in text:
            # Sicherstellung: nicht nur als Teil von "charlottenburg-wilmersdorf" ohne Ortsteil
            if ort == "charlottenburg" and "charlottenburg-wilmersdorf" in text:
                # Nur akzeptieren wenn Charlottenburg auch isoliert vorkommt
                pattern = r"\bcharlottenburg\b(?!-wilmersdorf)"
                if not re.search(pattern, text):
                    continue
            return True
    return False


def _hat_ziel_plz(text: str) -> bool:
    """Prüft auf bekannte PLZ der Zielorte."""
    for plz in _ZIEL_PLZ:
        if plz in text:
            return True
    return False
