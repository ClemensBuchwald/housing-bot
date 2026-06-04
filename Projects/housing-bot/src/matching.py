from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from src.config import Criteria
from src.models import Listing, MatchResult

logger = logging.getLogger(__name__)


# --- Pflichtfilter ---

def _filter_preis(listing: Listing, criteria: Criteria) -> Optional[str]:
    preis = listing.warmmiete
    limit = criteria.preis.warmmiete_max

    if preis is None:
        preis = listing.kaltmiete
        limit = criteria.preis.kaltmiete_max

    if preis is None or limit == 0:
        return None  # unbekannt → nicht filtern

    if preis > limit:
        return f"Preis {preis}€ > Limit {limit}€"
    return None


def _filter_flaeche(listing: Listing, criteria: Criteria) -> Optional[str]:
    min_qm = criteria.flaeche.min_qm
    if min_qm == 0 or listing.flaeche is None:
        return None
    if listing.flaeche < min_qm:
        return f"Fläche {listing.flaeche}m² < Minimum {min_qm}m²"
    return None


def _filter_zimmer(listing: Listing, criteria: Criteria) -> Optional[str]:
    zimmer = listing.zimmer_gerundet
    if zimmer is None:
        return None
    if criteria.zimmer.min > 0 and zimmer < criteria.zimmer.min:
        return f"Zimmer {zimmer} < Minimum {criteria.zimmer.min}"
    if criteria.zimmer.max > 0 and zimmer > criteria.zimmer.max:
        return f"Zimmer {zimmer} > Maximum {criteria.zimmer.max}"
    return None


def _filter_stadtteil(listing: Listing, criteria: Criteria) -> Optional[str]:
    if not criteria.ort.stadtteile_ausschluss:
        return None
    if listing.stadtteil and listing.stadtteil in criteria.ort.stadtteile_ausschluss:
        return f"Stadtteil '{listing.stadtteil}' ausgeschlossen"
    return None


def _filter_schluesselwoerter(listing: Listing, criteria: Criteria) -> Optional[str]:
    if not criteria.schluesselwoerter_ausschluss:
        return None
    titel_lower = listing.titel.lower()
    for wort in criteria.schluesselwoerter_ausschluss:
        if wort.lower() in titel_lower:
            return f"Ausschluss-Schlüsselwort gefunden: '{wort}'"
    return None


# --- Scoring ---

def _score_stadtteil(listing: Listing, criteria: Criteria) -> int:
    if listing.stadtteil and listing.stadtteil in criteria.ort.stadtteile_bevorzugt:
        return 20
    return 0


def _score_balkon(listing: Listing, criteria: Criteria) -> int:
    if criteria.ausstattung.balkon_oder_terrasse == "egal":
        return 0
    if listing.hat_merkmal("balkon", "terrasse", "loggia"):
        return 15
    return 0


def _score_preis(listing: Listing, criteria: Criteria) -> int:
    preis = listing.warmmiete or listing.kaltmiete
    limit = criteria.preis.warmmiete_max or criteria.preis.kaltmiete_max
    if preis is None or limit == 0:
        return 0
    # +10 wenn Preis mehr als 10% unter dem Limit liegt
    if preis <= limit * 0.90:
        return 10
    return 0


def _score_haustiere(listing: Listing, criteria: Criteria) -> int:
    if criteria.ausstattung.haustiere_erlaubt == "egal":
        return 0
    if listing.hat_merkmal("haustier", "haustiere erlaubt", "hunde", "katzen"):
        return 10
    return 0


def _score_einzug(listing: Listing, criteria: Criteria) -> int:
    if criteria.einzug_ab is None or listing.verfuegbar_ab is None:
        return 0
    if listing.verfuegbar_ab <= criteria.einzug_ab:
        return 10
    return 0


# --- Haupt-API ---

_PFLICHTFILTER = [
    _filter_preis,
    _filter_flaeche,
    _filter_zimmer,
    _filter_stadtteil,
    _filter_schluesselwoerter,
]

_SCORING = [
    _score_stadtteil,
    _score_balkon,
    _score_preis,
    _score_haustiere,
    _score_einzug,
]


def match(listing: Listing, criteria: Criteria) -> MatchResult:
    for f in _PFLICHTFILTER:
        grund = f(listing, criteria)
        if grund:
            logger.debug("Abgelehnt [%s]: %s", listing.id, grund)
            return MatchResult(listing=listing, bestanden=False, score=0, ablehnungsgrund=grund)

    score = sum(s(listing, criteria) for s in _SCORING)
    logger.debug("Match [%s] score=%d", listing.id, score)
    return MatchResult(listing=listing, bestanden=True, score=score)
