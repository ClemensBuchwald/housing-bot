"""Gemeinsame Testdoubles für die LLM-Schicht.

Alle Tests laufen ausschließlich gegen diese Attrappen — es gibt in der
gesamten Testsuite keinen echten Anthropic-Aufruf.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, List, Optional

from src.llm import factory
from src.llm.base import LLMResult, ToolCall


class FakeProvider:
    """Provider mit vorgegebenem Verhalten.

    ``verhalten`` ist eine Liste. Jeder Aufruf von ``complete`` nimmt den
    nächsten Eintrag:

      Exception  → wird geworfen
      LLMResult  → wird zurückgegeben
      dict       → wird als JSON-Text zurückgegeben (der Normalfall der Bewertung)
      str        → wird als Text zurückgegeben

    Der letzte Eintrag bleibt stehen und wiederholt sich, damit ein dauerhaft
    gestörter Anbieter nachgestellt werden kann.
    """

    name = "fake"
    model = "fake-modell"

    def __init__(self, verhalten: Optional[List[Any]] = None) -> None:
        self.verhalten: List[Any] = list(verhalten or [])
        self.aufrufe: List[dict] = []

    def complete(self, *, messages, system=None, tools=None, max_tokens=1024) -> LLMResult:
        self.aufrufe.append({
            "messages": messages, "system": system,
            "tools": tools, "max_tokens": max_tokens,
        })
        if not self.verhalten:
            return LLMResult(text="", stop_reason="end_turn")
        v = self.verhalten[0] if len(self.verhalten) == 1 else self.verhalten.pop(0)
        if isinstance(v, Exception):
            raise v
        if isinstance(v, LLMResult):
            return v
        if isinstance(v, dict):
            return LLMResult(text=json.dumps(v, ensure_ascii=False), stop_reason="end_turn")
        return LLMResult(text=str(v), stop_reason="end_turn")

    @property
    def anzahl_aufrufe(self) -> int:
        return len(self.aufrufe)


@contextmanager
def benutze_provider(provider):
    """Setzt den Provider für die Dauer des Blocks und stellt danach wieder her."""
    factory.set_provider(provider)
    try:
        yield provider
    finally:
        factory.set_provider(None)


def bewertung(passt: bool = True, score: int = 80, **extra) -> dict:
    """Eine gültige fachliche Bewertungsantwort des Modells."""
    d = {
        "passt": passt,
        "score": score,
        "kurzfazit": "Testbewertung",
        "vorteile": ["Balkon"],
        "nachteile": [],
        "offene_punkte": [],
        "empfehlung": "sofort anschauen" if passt else "überspringen",
    }
    d.update(extra)
    return d


def abgeschnitten(text: str = '{"passt": true, "score": 8') -> LLMResult:
    """Antwort, die ins Token-Limit gelaufen ist."""
    return LLMResult(text=text, stop_reason="max_tokens")


def werkzeugaufruf(name: str, eingabe: Optional[dict] = None,
                   tool_id: str = "tc-1") -> LLMResult:
    """Antwort, die ein Werkzeug anfordert."""
    eingabe = eingabe or {}
    return LLMResult(
        text="",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id=tool_id, name=name, input=eingabe)],
        raw_content=[{"type": "tool_use", "id": tool_id, "name": name, "input": eingabe}],
    )


def antwort(text: str) -> LLMResult:
    return LLMResult(text=text, stop_reason="end_turn")
