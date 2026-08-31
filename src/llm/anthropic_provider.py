"""Anthropic-Provider — die einzige derzeit implementierte Anbindung.

Aufgabe: das Anthropic-SDK kapseln und dessen Ausnahmen in die
Housing-Bot-eigenen Fehlerklassen übersetzen. Ausserhalb dieses Moduls
soll kein Code mehr anthropic.* kennen.

Das Verhalten entspricht funktional dem bisherigen direkten SDK-Aufruf.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

import anthropic

from src.llm.base import LLMResult, ToolCall
from src.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"

# Normalisierung der Anthropic-Abbruchgründe auf das Interface-Vokabular.
_STOP_REASONS = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
}


class AnthropicProvider:
    """Implementiert das LLMProvider-Protokoll."""

    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self.model = model or os.getenv("LLM_MODEL") or DEFAULT_MODEL
        self._api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self._timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT", "60"))
        if not self._api_key:
            # Konfigurationsfehler, kein transienter Fehler: Retry hilft nicht.
            raise LLMConfigError("ANTHROPIC_API_KEY fehlt in der Umgebung")

    def complete(
        self,
        *,
        messages: List[dict],
        system: Optional[str] = None,
        tools: Optional[List[dict]] = None,
        max_tokens: int = 1024,
    ) -> LLMResult:
        client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
        kwargs: dict = {"model": self.model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:                       # SDK-Ausnahmen uebersetzen
            raise _uebersetze(e) from e

        return _zu_result(resp)


def _zu_result(resp: Any) -> LLMResult:
    """Antwort normalisieren. Strukturprobleme sind Protokollfehler."""
    try:
        blocks = list(resp.content or [])
    except Exception as e:
        raise LLMProtocolError(f"Antwort ohne content: {e}") from e

    text = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text").strip()
    tool_calls = [
        ToolCall(id=b.id, name=b.name, input=dict(getattr(b, "input", {}) or {}))
        for b in blocks if getattr(b, "type", "") == "tool_use"
    ]
    raw_stop = getattr(resp, "stop_reason", None) or "other"
    return LLMResult(
        text=text,
        stop_reason=_STOP_REASONS.get(raw_stop, "other"),
        tool_calls=tool_calls,
        raw_content=blocks,
    )


def _uebersetze(e: Exception) -> LLMError:
    """SDK-Ausnahme -> Housing-Bot-Fehlerklasse.

    Bewusst defensiv über getattr/Statuscode statt über exakte Klassennamen,
    damit ein SDK-Update die Zuordnung nicht still bricht.
    """
    status = getattr(e, "status_code", None)
    name = type(e).__name__

    if name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        return LLMAuthError(f"Authentifizierung/Berechtigung fehlgeschlagen ({name})")
    if name == "RateLimitError" or status == 429:
        retry_after = None
        try:                                   # Retry-After weiterreichen, falls vorhanden
            retry_after = float(e.response.headers.get("retry-after"))  # type: ignore[attr-defined]
        except Exception:
            pass
        return LLMRateLimitError(f"Rate-Limit erreicht ({name})", retry_after=retry_after)
    if name in ("APIConnectionError", "APITimeoutError") or isinstance(e, (TimeoutError, ConnectionError)):
        return LLMTemporaryError(f"Verbindungsproblem ({name})")
    if status is not None and 500 <= int(status) < 600:
        return LLMTemporaryError(f"Serverfehler {status} ({name})")
    if status is not None and 400 <= int(status) < 500:
        # 400/404/422: falsche Anfrage oder unbekanntes Modell — Retry hilft nicht.
        return LLMConfigError(f"Ungueltige Anfrage {status} ({name}): {e}")
    return LLMTemporaryError(f"Unerwarteter Providerfehler ({name}): {e}")
