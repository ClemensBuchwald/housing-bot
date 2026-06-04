from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class Suche(BaseModel):
    typ: str = "wohnung"
    modus: str = "miete"


class Ort(BaseModel):
    staedte: List[str] = Field(default_factory=list)
    stadtteile_bevorzugt: List[str] = Field(default_factory=list)
    stadtteile_ausschluss: List[str] = Field(default_factory=list)
    umkreis_km: int = 0


class Preis(BaseModel):
    warmmiete_max: float = 0
    kaltmiete_max: float = 0


class Flaeche(BaseModel):
    min_qm: float = 0
    max_qm: float = 0


class Zimmer(BaseModel):
    min: float = 1
    max: float = 0


class Ausstattung(BaseModel):
    balkon_oder_terrasse: Literal["bevorzugt", "erforderlich", "egal"] = "egal"
    einbaukueche: Literal["bevorzugt", "erforderlich", "egal"] = "egal"
    keller: Literal["bevorzugt", "erforderlich", "egal"] = "egal"
    aufzug: Literal["bevorzugt", "erforderlich", "egal"] = "egal"
    barrierefrei: Literal["bevorzugt", "erforderlich", "egal"] = "egal"
    haustiere_erlaubt: Literal["bevorzugt", "erforderlich", "egal"] = "egal"


class Portale(BaseModel):
    immobilienscout24: bool = False
    immowelt: bool = False
    wg_gesucht: bool = False
    ebay_kleinanzeigen: bool = False


class Benachrichtigung(BaseModel):
    telegram: bool = True
    email: bool = False
    nur_neue: bool = True


class Criteria(BaseModel):
    suche: Suche = Field(default_factory=Suche)
    ort: Ort = Field(default_factory=Ort)
    preis: Preis = Field(default_factory=Preis)
    flaeche: Flaeche = Field(default_factory=Flaeche)
    zimmer: Zimmer = Field(default_factory=Zimmer)
    ausstattung: Ausstattung = Field(default_factory=Ausstattung)
    schluesselwoerter_ausschluss: List[str] = Field(default_factory=list)
    einzug_ab: Optional[date] = None
    portale: Portale = Field(default_factory=Portale)
    benachrichtigung: Benachrichtigung = Field(default_factory=Benachrichtigung)


def load_criteria(path: Path | str | None = None) -> Criteria:
    if path is None:
        path = Path(__file__).parent.parent / "config" / "criteria.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Criteria.model_validate(data)
