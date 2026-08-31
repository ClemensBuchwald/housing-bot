"""Housing-Bot-eigene LLM-Schicht.

Fachlogik → LLMProvider-Interface → AnthropicProvider.

Bewusst projekteigen: keine gemeinsame Runtime mit anderen Projekten, kein
externer Runner, keine Weiterleitung von Anfragen über fremde Dienste.
"""
from src.llm.base import LLMProvider, LLMResult, ToolCall
from src.llm.circuit import CircuitBreaker, CircuitProvider
from src.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)
from src.llm.factory import get_breaker, get_provider, set_provider

__all__ = [
    "LLMProvider", "LLMResult", "ToolCall",
    "LLMError", "LLMAuthError", "LLMRateLimitError",
    "LLMTemporaryError", "LLMProtocolError", "LLMConfigError",
    "get_provider", "set_provider", "get_breaker",
    "CircuitBreaker", "CircuitProvider",
]
