"""Claude-basierte Auftragsstrukturierung und Inserat-Bewertung.

Zwei Funktionen:
  parse_mandate()    — Freitext-Suchauftrag → strukturiertes JSON
  evaluate_listing() — Inserat + Auftrag → Bewertung mit Vor-/Nachteilen

Modell: claude-haiku-4-5 (schnell + günstig, für häufige Aufrufe)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

import anthropic

from src.models import Listing

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"

_PARSE_MANDATE_PROMPT = """\
Du bist ein Assistent der hilft, Wohnungs-Suchaufträge zu verstehen und zu strukturieren.

Der Nutzer hat folgenden Suchauftrag formuliert:
<auftrag>
{raw_text}
</auftrag>

Extrahiere daraus ein strukturiertes Suchprofil als JSON. Felder:
- zielorte: Liste der gewünschten Stadtteile/Orte (leer = überall)
- warmmiete_max: maximale Warmmiete in EUR (null = kein Limit)
- kaltmiete_max: maximale Kaltmiete in EUR (null = kein Limit)
- zimmer_min: Mindestanzahl Zimmer (null = egal)
- zimmer_max: Maximale Zimmer (null = egal)
- flaeche_min: Mindestfläche in m² (null = egal)
- ausschlusskriterien: Liste harter Ausschlusskriterien (z.B. ["Erdgeschoss", "WBS erforderlich"])
- wunschkriterien: Liste weicher Wünsche (z.B. ["Balkon", "Einbauküche"])
- sonstiges: Freitext für alles was nicht in obige Felder passt

Antworte NUR mit gültigem JSON, ohne Erklärung."""

_EVALUATE_LISTING_PROMPT = """\
Du bist ein Wohnungs-Suchassistent. Bewerte das folgende Inserat gegen den Suchauftrag.

SUCHAUFTRAG:
{mandate_text}

STRUKTURIERTER AUFTRAG:
{mandate_structured}

INSERAT:
Titel: {titel}
Quelle: {portal}
Ort: {ort}
Kaltmiete: {kaltmiete}
Warmmiete: {warmmiete}
Fläche: {flaeche}
Zimmer: {zimmer}
Merkmale: {merkmale}
Link: {url}

Bewerte das Inserat und antworte NUR mit folgendem JSON:
{{
  "passt": true/false,
  "score": 0-100,
  "kurzfazit": "Ein Satz",
  "vorteile": ["Vorteil 1", "Vorteil 2"],
  "nachteile": ["Nachteil 1"],
  "offene_punkte": ["Unklarheit 1"],
  "empfehlung": "sofort anschauen" | "beobachten" | "überspringen"
}}

Wichtige Hinweise:
- "passt: false" wenn ein hartes Ausschlusskriterium zutrifft
- Score 0-100: 0 = gar nicht passend, 100 = perfekter Treffer
- Nutze Textverständnis: "Hochparterre" kann Erdgeschoss bedeuten
- Fehlende Daten (Warmmiete unbekannt, Etage unklar) als offene_punkte benennen
- Sei präzise und ehrlich, keine leeren Lobhudeleien
- Antworte NUR mit JSON, ohne Erklärung"""


@dataclass
class Evaluation:
    passt: bool
    score: int
    kurzfazit: str
    vorteile: List[str] = field(default_factory=list)
    nachteile: List[str] = field(default_factory=list)
    offene_punkte: List[str] = field(default_factory=list)
    empfehlung: str = "beobachten"

    @classmethod
    def from_dict(cls, d: dict) -> "Evaluation":
        return cls(
            passt=bool(d.get("passt", False)),
            score=int(d.get("score", 0)),
            kurzfazit=str(d.get("kurzfazit", "")),
            vorteile=d.get("vorteile", []),
            nachteile=d.get("nachteile", []),
            offene_punkte=d.get("offene_punkte", []),
            empfehlung=d.get("empfehlung", "beobachten"),
        )

    @classmethod
    def fallback(cls, reason: str) -> "Evaluation":
        return cls(passt=False, score=0, kurzfazit=reason, empfehlung="überspringen")


def _client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
    return anthropic.Anthropic(api_key=api_key)


def parse_mandate(raw_text: str) -> dict:
    """Konvertiert Freitext-Suchauftrag in strukturiertes JSON via Claude."""
    try:
        client = _client()
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": _PARSE_MANDATE_PROMPT.format(raw_text=raw_text),
            }],
        )
        text = msg.content[0].text.strip()
        # JSON aus Antwort extrahieren
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Mandate-Parse fehlgeschlagen: %s", e)
        return {"sonstiges": raw_text}


def evaluate_listing(listing: Listing, mandate: dict) -> Evaluation:
    """Bewertet ein Inserat gegen den aktiven Suchauftrag via Claude."""
    try:
        client = _client()

        def fmt(v) -> str:
            return str(v) if v is not None else "unbekannt"

        prompt = _EVALUATE_LISTING_PROMPT.format(
            mandate_text=mandate.get("raw_text", ""),
            mandate_structured=json.dumps(mandate.get("structured", {}), ensure_ascii=False, indent=2),
            titel=listing.titel,
            portal=listing.portal,
            ort=f"{listing.stadtteil or ''}, {listing.stadt}".strip(", "),
            kaltmiete=fmt(listing.kaltmiete) + " €" if listing.kaltmiete else "unbekannt",
            warmmiete=fmt(listing.warmmiete) + " €" if listing.warmmiete else "unbekannt",
            flaeche=fmt(listing.flaeche) + " m²" if listing.flaeche else "unbekannt",
            zimmer=fmt(listing.zimmer) if listing.zimmer else "unbekannt",
            merkmale=", ".join(listing.merkmale) if listing.merkmale else "keine",
            url=listing.url,
        )

        msg = client.messages.create(
            model=_MODEL,
            max_tokens=768,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()

        return Evaluation.from_dict(json.loads(text))

    except Exception as e:
        logger.error("Listing-Evaluation fehlgeschlagen: %s", e)
        return Evaluation.fallback(f"Bewertung nicht möglich: {e}")
