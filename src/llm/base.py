"""Provider-Interface der Housing-Bot-eigenen LLM-Schicht.

Die Fachlogik (evaluator, agent) kennt ab hier nur noch dieses Interface und die
Fehlerklassen aus errors.py — nicht mehr das Anthropic-SDK.

Diese Schicht gehört ausschließlich zum Housing Bot. Sie hat keine Verbindung zu
anderen Projekten und teilt keine Runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:                                     # Python 3.8+: Protocol aus typing
    from typing import Protocol, runtime_checkable
except ImportError:                      # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore


@dataclass
class ToolCall:
    """Ein vom Modell angeforderter Werkzeugaufruf, provider-unabhängig."""

    id: str
    name: str
    input: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    """Antwort eines Providers.

    text        — zusammengefügte Textblöcke
    stop_reason — normalisiert: "end_turn" | "tool_use" | "max_tokens" | "other"
    tool_calls  — normalisierte Werkzeugaufrufe
    raw_content — providereigene Rohblöcke; wird für die Tool-Schleife unverändert
                  an den Provider zurückgereicht. Die Fachlogik interpretiert sie nicht.
    """

    text: str
    stop_reason: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw_content: Any = None


@runtime_checkable
class LLMProvider(Protocol):
    """Minimales Interface. Bewusst generisch statt fachlich geschnitten —
    die Fachprompts bleiben in evaluator.py bzw. agent.py."""

    name: str
    model: str

    def complete(
        self,
        *,
        messages: List[dict],
        system: Optional[str] = None,
        tools: Optional[List[dict]] = None,
        max_tokens: int = 1024,
    ) -> LLMResult:
        """Führt eine Anfrage aus.

        Wirft ausschließlich LLMError-Unterklassen (siehe errors.py) —
        niemals providerspezifische Ausnahmen.
        """
        ...
