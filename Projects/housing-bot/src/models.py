from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Literal, Optional


@dataclass
class Listing:
    id: str
    portal: Literal["is24", "immowelt", "ebay", "wg_gesucht", "mock"]
    url: str
    titel: str
    stadt: str
    kaltmiete: Optional[float] = None
    warmmiete: Optional[float] = None
    flaeche: Optional[float] = None
    zimmer: Optional[float] = None
    stadtteil: Optional[str] = None
    verfuegbar_ab: Optional[date] = None
    merkmale: List[str] = field(default_factory=list)
    gefunden_am: datetime = field(default_factory=datetime.now)

    @property
    def relevanter_preis(self) -> Optional[float]:
        """Warmmiete bevorzugt, Kaltmiete als Fallback."""
        return self.warmmiete if self.warmmiete is not None else self.kaltmiete

    @property
    def zimmer_gerundet(self) -> Optional[int]:
        """Halbe Zimmer werden aufgerundet (2,5 → 3)."""
        if self.zimmer is None:
            return None
        return math.ceil(self.zimmer)

    def hat_merkmal(self, *schluessel: str) -> bool:
        text = " ".join(self.merkmale).lower()
        return any(s.lower() in text for s in schluessel)


@dataclass
class MatchResult:
    listing: Listing
    bestanden: bool
    score: int
    ablehnungsgrund: Optional[str] = None
