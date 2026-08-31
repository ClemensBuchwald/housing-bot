"""Fehlerklassen der Housing-Bot-eigenen LLM-Schicht.

Kernidee: Ein technischer Fehler ist KEINE fachliche Aussage. Er darf niemals als
"passt nicht" gespeichert werden, sondern bedeutet "keine Entscheidung".

Die Retry-Fähigkeit hängt als explizites Attribut am Fehler — nicht als implizites
Wissen beim Aufrufer. Zwei Eigenschaften werden getrennt geführt, weil sie sich
unterschiedlich verhalten:

  retryable          — kann ein erneuter Versuch überhaupt helfen?
  verbraucht_versuch — zählt ein Fehlversuch gegen das Kontingent?
  max_versuche       — wie viele WIEDERHOLUNGEN nach dem ersten Fehlschlag noch
                       zulässig sind (nicht: Gesamtzahl der Versuche)

Wichtigster Fall: LLMAuthError heilt nie von selbst. Er ist nicht retryable UND
verbraucht keinen Versuch — sonst wäre die Warteschlange leer, bevor jemand den
Key erneuern konnte, und die Inserate der Nacht wären endgültig verloren.
"""
from __future__ import annotations

from typing import Optional


class LLMError(Exception):
    """Basis: technischer Fehler der LLM-Schicht (nie ein fachliches Ergebnis)."""

    retryable: bool = True
    verbraucht_versuch: bool = True
    max_versuche: int = 3
    kategorie: str = "unbekannt"


class LLMAuthError(LLMError):
    """Ungültiger/fehlender Key, fehlende Berechtigung, Guthaben erschöpft."""

    retryable = False
    verbraucht_versuch = False      # heilt nicht von selbst -> Eintrag bleibt liegen
    max_versuche = 0
    kategorie = "auth"


class LLMRateLimitError(LLMError):
    """429 — vorübergehend, Backoff hilft."""

    retryable = True
    verbraucht_versuch = True
    max_versuche = 5
    kategorie = "ratelimit"

    def __init__(self, *args, retry_after: Optional[float] = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class LLMTemporaryError(LLMError):
    """5xx, Timeout, Verbindungsabbruch — klassisch transient."""

    retryable = True
    verbraucht_versuch = True
    max_versuche = 3
    kategorie = "temporaer"


class LLMProtocolError(LLMError):
    """Antwort unbrauchbar (kaputtes JSON, leerer Inhalt, unerwartete Struktur).

    Bei gleichem Prompt weitgehend deterministisch — deshalb nur EIN weiterer
    Versuch, sonst kostet dieselbe Antwort mehrfach Tokens.
    """

    retryable = True
    verbraucht_versuch = True
    max_versuche = 1
    kategorie = "protokoll"


class LLMConfigError(LLMError):
    """Fehlkonfiguration (unbekannter Provider, fehlendes Modell)."""

    retryable = False
    verbraucht_versuch = False
    max_versuche = 0
    kategorie = "konfiguration"


def zu_llm_fehler(e: Exception) -> LLMError:
    """Beliebige Ausnahme in die Fehlersystematik einordnen.

    Auch ein Programmierfehler ist keine fachliche Entscheidung. Er wird wie ein
    vorübergehender Fehler behandelt: Das Inserat bleibt in der Warteschlange und
    wird nie als "passt nicht" verbucht.
    """
    if isinstance(e, LLMError):
        return e
    return LLMTemporaryError("Unerwarteter Fehler (%s): %s" % (type(e).__name__, e))
