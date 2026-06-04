from __future__ import annotations

from datetime import date

from typing import List

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper

_MOCK_LISTINGS = [
    Listing(
        id="mock-001",
        portal="mock",
        url="https://example.com/inserat/1",
        titel="Schöne 2-Zimmer-Wohnung in Prenzlauer Berg",
        stadt="Berlin",
        stadtteil="Prenzlauer Berg",
        kaltmiete=950.0,
        warmmiete=1150.0,
        flaeche=62.0,
        zimmer=2.0,
        verfuegbar_ab=date(2026, 8, 1),
        merkmale=["Balkon", "Einbauküche", "Haustiere erlaubt"],
    ),
    Listing(
        id="mock-002",
        portal="mock",
        url="https://example.com/inserat/2",
        titel="Helle 3-Zimmer mit Terrasse, Friedrichshain",
        stadt="Berlin",
        stadtteil="Friedrichshain",
        kaltmiete=1100.0,
        warmmiete=1380.0,
        flaeche=78.0,
        zimmer=3.0,
        verfuegbar_ab=date(2026, 7, 1),
        merkmale=["Terrasse", "Keller"],
    ),
    Listing(
        id="mock-003",
        portal="mock",
        url="https://example.com/inserat/3",
        titel="Zwischenmiete 2 Zimmer Mitte",
        stadt="Berlin",
        stadtteil="Mitte",
        kaltmiete=800.0,
        warmmiete=950.0,
        flaeche=55.0,
        zimmer=2.0,
        verfuegbar_ab=date(2026, 7, 15),
        merkmale=[],
    ),
    Listing(
        id="mock-004",
        portal="mock",
        url="https://example.com/inserat/4",
        titel="Luxus-Loft Charlottenburg",
        stadt="Berlin",
        stadtteil="Charlottenburg",
        kaltmiete=2200.0,
        warmmiete=2600.0,
        flaeche=120.0,
        zimmer=3.0,
        merkmale=["Balkon", "Concierge"],
    ),
    Listing(
        id="mock-005",
        portal="mock",
        url="https://example.com/inserat/5",
        titel="Gemütliche 2-Zimmer in Neukölln",
        stadt="Berlin",
        stadtteil="Neukölln",
        kaltmiete=880.0,
        warmmiete=1080.0,
        flaeche=54.0,
        zimmer=2.0,
        verfuegbar_ab=date(2026, 7, 1),
        merkmale=["Haustiere erlaubt"],
    ),
]


class MockScraper(BaseScraper):
    name = "mock"

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        return list(_MOCK_LISTINGS)
