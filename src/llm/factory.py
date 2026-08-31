"""Provider-Auswahl für den Housing Bot.

Konfiguration über Umgebungsvariablen:
  LLM_PROVIDER  — derzeit nur "anthropic" (Vorgabe)
  LLM_MODEL     — Modellname (Vorgabe: claude-haiku-4-5)
  LLM_TIMEOUT   — Sekunden (Vorgabe: 60)

Bewusst ohne Registry-Magie: solange es genau einen Provider gibt, wäre alles
andere Overhead. Ein zweiter Provider ist ein weiterer elif — die Fachlogik
bleibt davon unberührt.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from src.llm.base import LLMProvider
from src.llm.circuit import CircuitBreaker, CircuitProvider
from src.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_provider: Optional[LLMProvider] = None


def get_provider(force_new: bool = False) -> LLMProvider:
    """Liefert den konfigurierten Provider (Singleton, thread-sicher)."""
    global _provider
    with _lock:
        if _provider is None or force_new:
            _provider = _erzeuge()
        return _provider


def set_provider(provider: Optional[LLMProvider]) -> None:
    """Setzt den Provider — ausschliesslich für Tests (Fake-Provider)."""
    global _provider
    with _lock:
        _provider = provider


def get_breaker() -> Optional[CircuitBreaker]:
    """Der Stromkreis des aktiven Providers — None, wenn keiner vorhanden ist
    (etwa wenn Tests einen nackten Provider gesetzt haben)."""
    with _lock:
        return getattr(_provider, "breaker", None)


def _erzeuge() -> LLMProvider:
    name = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    if name == "anthropic":
        from src.llm.anthropic_provider import AnthropicProvider
        p = AnthropicProvider()
        logger.info("LLM-Provider: %s (Modell %s)", p.name, p.model)
        # Umhüllt: Ein Ausfall darf nicht zu einem Aufruf je Inserat führen.
        return CircuitProvider(p)
    raise LLMConfigError(f"Unbekannter LLM_PROVIDER: {name!r}")
