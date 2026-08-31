"""Claude-basierte Auftragsstrukturierung und Inserat-Bewertung.

Zwei Funktionen:
  parse_mandate()    — Freitext-Suchauftrag → strukturiertes JSON
  evaluate_listing() — Inserat + Auftrag → Bewertung mit Vor-/Nachteilen

Beide sprechen ausschließlich über src.llm mit dem Modell — kein direkter
SDK-Zugriff mehr.

Trennung von Sachurteil und Störung
-----------------------------------
``Evaluation.passt = False`` heißt genau eines: Das Modell hat das Inserat
geprüft und für unpassend befunden.

Eine technische Störung ist KEINE Entscheidung. Beide Funktionen werfen dann
einen LLMError, statt eine negative Bewertung zurückzugeben. Früher wurde jeder
Fehler — abgelaufener Schlüssel, Zeitüberschreitung, abgeschnittenes JSON — zu
``passt=False`` mit Score 0 und landete als endgültiges "abgelehnt" in der
Datenbank. Ein einziger ungültiger Schlüssel konnte so eine ganze Nacht an
Inseraten stillschweigend verwerfen.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, List

from src.llm import get_provider
from src.llm.errors import LLMProtocolError
from src.models import Listing

logger = logging.getLogger(__name__)

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
    """Ein FACHLICHES Urteil des Modells. Entsteht nie aus einem Fehlerpfad."""

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


def _merkmale_kurz(merkmale, max_beschreibung: int = 320) -> str:
    """Merkmale kompakt für den Prompt — Kostenbremse.

    Die Objektbeschreibung ist mit Abstand das längste Feld und trieb die
    Eingabe-Token je Bewertung stark hoch. Sie wird gekürzt; die kurzen
    Fakten-Merkmale (Etage, Balkon, PLZ …) bleiben vollständig, weil genau
    sie für die Kriterienprüfung gebraucht werden.
    """
    if not merkmale:
        return "keine"
    fakten, beschreibung = [], ""
    for m in merkmale:
        s = str(m)
        if s.startswith("Beschreibung:"):
            beschreibung = s[len("Beschreibung:"):][:max_beschreibung]
        else:
            fakten.append(s)
    teile = ", ".join(fakten)
    if beschreibung:
        teile += f" | Beschreibung: {beschreibung}"
    return teile or "keine"


def _json_aus_antwort(text: str, stop_reason: str, kontext: str) -> Any:
    """Antworttext zu JSON. Jede Unbrauchbarkeit ist ein Protokollfehler.

    Der abgeschnittene Fall ist der heimtückische: Bei ``max_tokens`` bricht das
    JSON mitten im Satz ab. Das ist keine Ablehnung, sondern eine unvollständige
    Antwort — und muss als solche erkennbar bleiben.
    """
    if stop_reason == "max_tokens":
        raise LLMProtocolError(f"{kontext}: Antwort am Token-Limit abgeschnitten")
    if not text:
        raise LLMProtocolError(f"{kontext}: leere Antwort")

    roh = text.strip()
    if "```" in roh:
        teile = roh.split("```")
        if len(teile) < 2:
            raise LLMProtocolError(f"{kontext}: unvollständiger Code-Block")
        roh = teile[1]
        if roh.startswith("json"):
            roh = roh[4:]
        roh = roh.strip()

    try:
        return json.loads(roh)
    except (ValueError, TypeError) as e:
        raise LLMProtocolError(f"{kontext}: Antwort ist kein gültiges JSON ({e})") from e


def parse_mandate(raw_text: str) -> dict:
    """Konvertiert Freitext-Suchauftrag in strukturiertes JSON.

    Wirft bei technischer Störung einen LLMError. Früher wurde in diesem Fall
    ``{"sonstiges": raw_text}`` zurückgegeben — ein Auftrag ohne Zielorte, ohne
    Preisgrenze, ohne Ausschlusskriterien, der dem Nutzer als gespeichert
    bestätigt wurde und danach alles oder nichts durchgelassen hätte.
    """
    provider = get_provider()
    result = provider.complete(
        messages=[{"role": "user", "content": _PARSE_MANDATE_PROMPT.format(raw_text=raw_text)}],
        max_tokens=512,
    )
    daten = _json_aus_antwort(result.text, result.stop_reason, "Auftragsanalyse")
    if not isinstance(daten, dict):
        raise LLMProtocolError("Auftragsanalyse: JSON ist kein Objekt")
    return daten


def evaluate_listing(listing: Listing, mandate: dict) -> Evaluation:
    """Bewertet ein Inserat gegen den aktiven Suchauftrag.

    Rückgabe = fachliches Urteil des Modells.
    Ausnahme  = keine Entscheidung; der Aufrufer muss das Inserat bewertbar halten.
    """
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
        merkmale=_merkmale_kurz(listing.merkmale),
        url=listing.url,
    )

    provider = get_provider()
    result = provider.complete(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=768,
    )
    daten = _json_aus_antwort(result.text, result.stop_reason, f"Bewertung {listing.id}")
    if not isinstance(daten, dict):
        raise LLMProtocolError(f"Bewertung {listing.id}: JSON ist kein Objekt")
    return Evaluation.from_dict(daten)
