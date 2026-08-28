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

# Ortsteile im selben Bezirk (Charlottenburg-Wilmersdorf), die NICHT gesucht sind.
# Ihre PLZ überlappen teils mit den Zielorten — der benannte Ortsteil hat Vorrang.
_NICHT_ZIELORTE = {
    "westend", "schmargendorf", "charlottenburg-nord", "grunewald-forst",
}


def in_zielgebiet(listing: "Listing") -> bool:
    """True wenn das Listing eindeutig einem Zielort zugeordnet werden kann.

    Ortsnamen dürfen auch aus der URL kommen (z.B. '/berlin-charlottenburg/').
    Die PLZ-Prüfung ignoriert die URL bewusst — dort stehen Anzeigen-IDs, deren
    Ziffernfolgen sonst zufällig eine Ziel-PLZ treffen können.

    Ein ausdrücklich erkannter Nicht-Zielortsteil (z.B. Westend, Schmargendorf)
    schlägt die PLZ-Heuristik: Die PLZ-Bereiche überlappen an den Bezirksrändern,
    ein konkret benannter Ortsteil ist die verlässlichere Angabe.
    """
    if listing.stadtteil:
        st = listing.stadtteil.strip().lower()
        if st in _ZIELORTE:
            return True
        if st in _NICHT_ZIELORTE:
            return False
    return _hat_zielort(_listing_text(listing)) or _hat_ziel_plz(_listing_text(listing, mit_url=False))


# Doppel-Bezirksnamen: enthalten Ortsteilnamen, sind aber KEIN Ortsteil.
# Ohne Maskierung wird z.B. jedes Inserat im Bezirk "Charlottenburg-Wilmersdorf"
# fälschlich als Ortsteil "Wilmersdorf" gelesen.
_BEZIRKSNAMEN = [
    "charlottenburg-wilmersdorf", "charlottenburg-nord",
    "steglitz-zehlendorf", "tempelhof-schöneberg", "tempelhof-schoeneberg",
    "marzahn-hellersdorf", "friedrichshain-kreuzberg", "treptow-köpenick",
    "treptow-koepenick", "mitte-tiergarten",
]


def extract_stadtteil(text: str) -> Optional[str]:
    """Extrahiert den spezifischsten erkennbaren Stadtteil aus einem Text."""
    text_lower = text.lower()
    # Bezirksnamen ausblenden, damit sie nicht als Ortsteil durchgehen
    for bez in _BEZIRKSNAMEN:
        text_lower = text_lower.replace(bez, " ")

    # Priorität: Halensee und Grunewald zuerst (werden sonst von Charlottenburg verschluckt)
    for ort in ["halensee", "grunewald", "wilmersdorf", "charlottenburg"]:
        if ort in text_lower:
            return ort.capitalize()

    # Weitere Berliner Stadtteile als Fallback
    weitere = [
        # Nachbar-Ortsteile im selben Bezirk zuerst — sonst greift unten
        # fälschlich ein Zielort über die PLZ
        "westend", "schmargendorf",
        "mitte", "prenzlauer berg", "friedrichshain", "kreuzberg",
        "schöneberg", "tempelhof", "neukölln", "treptow", "pankow",
        "weißensee", "lichtenberg", "marzahn", "hellersdorf",
        "spandau", "reinickendorf", "steglitz", "zehlendorf",
    ]
    for ort in weitere:
        if ort in text_lower:
            return ort.title()

    return None


def _listing_text(listing: "Listing", mit_url: bool = True) -> str:
    parts = [
        listing.titel or "",
        listing.stadtteil or "",
        listing.stadt or "",
    ]
    if mit_url:
        parts.append(listing.url or "")
    # Merkmale enthalten bei mehreren Quellen die Adresse und "PLZ:xxxxx" —
    # ohne sie liefen PLZ-basierte Zuordnungen ins Leere.
    parts += [str(m) for m in (listing.merkmale or [])]
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
    """Prüft auf bekannte PLZ der Zielorte.

    Mit Wortgrenzen — ein nacktes Substring-Match traf sonst zufällige
    Ziffernfolgen in URLs/Anzeigen-IDs (z.B. '/s-anzeige/...10719...').
    """
    return bool(re.search(r"\b(" + "|".join(sorted(_ZIEL_PLZ)) + r")\b", text))
