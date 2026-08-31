"""Sicherung gegen Anrufstürme beim LLM-Anbieter.

Ohne diese Sicherung führt ein Ausfall zu einem Aufruf je Inserat: Bei einem
abgelaufenen Schlüssel und 150 Inseraten aus einer einzigen Quelle sind das 150
zwecklose Anfragen — je Zyklus, alle zwei Minuten.

Aufbau als Umhüllung des Providers, nicht als Sonderfall an jeder Aufrufstelle.
Ist der Stromkreis offen, wirft ``complete()`` sofort denselben Fehlertyp, der
ihn geöffnet hat. Damit greift die vorhandene Fehlersystematik unverändert
weiter: Ein Auth-Ausfall führt zu ``blocked`` ohne Versuchsverbrauch, ein
Rate-Limit zu ``pending`` mit Wartezeit. Es entsteht keine zweite Logik.

Drei Verhaltensweisen, weil die Ursachen sich grundlegend unterscheiden:

  Auth/Konfiguration   Heilt nicht von selbst. Der Stromkreis bleibt für die
                       Laufzeit des Prozesses offen; die Freigabe kommt über
                       den Neustart nach einer Korrektur (die .env wird ohnehin
                       nur beim Recreate neu gelesen).
  Rate-Limit           Die Gegenseite nennt die Wartezeit. Genau so lange
                       gesperrt, gedeckelt.
  Vorübergehend        Erst nach mehreren Fehlschlägen hintereinander sperren —
                       ein einzelner Verbindungsabbruch ist normal. Danach
                       Abkühlphase, dann ein einzelner Testaufruf.

Protokollfehler zählen bewusst NICHT auf die Serie: Ein unbrauchbares JSON ist
eine Eigenschaft der einzelnen Antwort, kein Zeichen für einen Anbieterausfall.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, List, Optional

from src.llm.base import LLMResult
from src.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMTemporaryError,
)

logger = logging.getLogger(__name__)

GESCHLOSSEN = "geschlossen"
OFFEN = "offen"
HALB_OFFEN = "halb_offen"

# Nach so vielen vorübergehenden Fehlern hintereinander wird gesperrt.
FEHLERSERIE_SCHWELLE = 4
# Erste Abkühlphase; verdoppelt sich, wenn der Testaufruf erneut scheitert.
COOLDOWN_START_S = 60.0
COOLDOWN_MAX_S = 900.0
# Obergrenze für ein Retry-After der Gegenseite.
RATE_LIMIT_DECKEL_S = 900.0


class CircuitBreaker:
    """Zustandsautomat. Thread-sicher, weil Scraping- und Telegram-Thread
    denselben Provider benutzen."""

    def __init__(self, schwelle: int = FEHLERSERIE_SCHWELLE,
                 cooldown_s: float = COOLDOWN_START_S) -> None:
        self._lock = threading.Lock()
        self._schwelle = schwelle
        self._cooldown_start = cooldown_s
        self._reset_intern()

    def _reset_intern(self) -> None:
        self._serie = 0
        self._gesperrt_bis: Optional[float] = None
        self._grund: Optional[LLMError] = None
        self._dauerhaft = False
        self._cooldown = self._cooldown_start
        self._probe_laeuft = False
        self._unterdrueckt = 0
        self._geoeffnet_am: Optional[float] = None

    # --- Abfrage ----------------------------------------------------------

    def blockiert(self) -> Optional[LLMError]:
        """Gibt den Fehler zurück, mit dem abzuweisen ist — oder None.

        Läuft die Abkühlphase gerade ab, wird genau EIN Aufruf durchgelassen
        (halb offen). Alle weiteren warten, bis dessen Ergebnis vorliegt.
        """
        with self._lock:
            if self._dauerhaft:
                self._unterdrueckt += 1
                return self._grund

            if self._gesperrt_bis is None:
                return None

            rest = self._gesperrt_bis - time.time()
            if rest > 0:
                self._unterdrueckt += 1
                return self._mit_restzeit(rest)

            if self._probe_laeuft:
                # Ein Testaufruf ist schon unterwegs — nicht noch einen schicken.
                self._unterdrueckt += 1
                return self._grund
            self._probe_laeuft = True
            logger.info("LLM-Stromkreis halb offen — ein Testaufruf wird zugelassen")
            return None

    def _mit_restzeit(self, rest: float) -> LLMError:
        """Beim Rate-Limit die verbleibende Wartezeit mitgeben, damit der
        Warteschlangeneintrag nicht zu früh wieder fällig wird."""
        if isinstance(self._grund, LLMRateLimitError):
            return LLMRateLimitError(str(self._grund), retry_after=rest)
        return self._grund if self._grund else LLMTemporaryError("Stromkreis offen")

    def zustand(self) -> str:
        with self._lock:
            if self._dauerhaft:
                return OFFEN
            if self._gesperrt_bis is None:
                return GESCHLOSSEN
            if self._gesperrt_bis - time.time() > 0:
                return OFFEN
            return HALB_OFFEN

    def info(self) -> dict:
        """Kompakter Zustand für die Gesundheitsanzeige — ohne Geheimnisse."""
        with self._lock:
            return {
                "zustand": (OFFEN if self._dauerhaft else
                            GESCHLOSSEN if self._gesperrt_bis is None else
                            OFFEN if self._gesperrt_bis - time.time() > 0 else HALB_OFFEN),
                "kategorie": self._grund.kategorie if self._grund else None,
                "dauerhaft": self._dauerhaft,
                "fehlerserie": self._serie,
                "gesperrt_noch_s": (max(0, int(self._gesperrt_bis - time.time()))
                                    if self._gesperrt_bis else 0),
                "unterdrueckte_aufrufe": self._unterdrueckt,
            }

    # --- Rückmeldung ------------------------------------------------------

    def melde_erfolg(self) -> None:
        with self._lock:
            if self._gesperrt_bis is not None or self._serie:
                logger.info("LLM-Stromkreis wieder geschlossen (%d Aufrufe unterdrückt)",
                            self._unterdrueckt)
            self._serie = 0
            self._gesperrt_bis = None
            self._grund = None
            self._cooldown = self._cooldown_start
            self._probe_laeuft = False
            self._unterdrueckt = 0
            self._geoeffnet_am = None

    def melde_fehler(self, e: LLMError) -> None:
        with self._lock:
            war_probe = self._probe_laeuft
            self._probe_laeuft = False

            if isinstance(e, (LLMAuthError, LLMConfigError)):
                if not self._dauerhaft:
                    logger.error("LLM-Stromkreis dauerhaft geöffnet (%s) — bis zum "
                                 "Neustart werden keine Bewertungen mehr versucht",
                                 e.kategorie)
                self._dauerhaft = True
                self._grund = e
                self._geoeffnet_am = self._geoeffnet_am or time.time()
                return

            if isinstance(e, LLMRateLimitError):
                warten = min(float(getattr(e, "retry_after", None) or 60.0), RATE_LIMIT_DECKEL_S)
                self._grund = e
                self._gesperrt_bis = time.time() + warten
                self._geoeffnet_am = self._geoeffnet_am or time.time()
                logger.warning("LLM-Stromkreis für %.0fs geöffnet (Rate-Limit)", warten)
                return

            if not isinstance(e, LLMTemporaryError):
                # Protokollfehler u. ä.: Eigenschaft der Antwort, kein Ausfall.
                return

            if war_probe:
                # Der Testaufruf ist gescheitert: länger warten.
                self._cooldown = min(self._cooldown * 2, COOLDOWN_MAX_S)
                self._gesperrt_bis = time.time() + self._cooldown
                self._grund = e
                logger.warning("LLM-Testaufruf gescheitert — erneut %.0fs gesperrt", self._cooldown)
                return

            self._serie += 1
            if self._serie >= self._schwelle:
                self._grund = e
                self._gesperrt_bis = time.time() + self._cooldown
                self._geoeffnet_am = self._geoeffnet_am or time.time()
                logger.warning("LLM-Stromkreis für %.0fs geöffnet (%d Fehler hintereinander)",
                               self._cooldown, self._serie)

    def reset(self) -> None:
        """Vollständige Freigabe — beim Prozessstart, nicht im laufenden Betrieb."""
        with self._lock:
            self._reset_intern()


class CircuitProvider:
    """Umhüllt einen Provider mit dem Stromkreis. Erfüllt dasselbe Protokoll."""

    def __init__(self, inner, breaker: Optional[CircuitBreaker] = None) -> None:
        self._inner = inner
        self.breaker = breaker or CircuitBreaker()

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "unbekannt")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unbekannt")

    def complete(self, *, messages: List[dict], system: Optional[str] = None,
                 tools: Optional[List[dict]] = None, max_tokens: int = 1024) -> LLMResult:
        gesperrt = self.breaker.blockiert()
        if gesperrt is not None:
            raise gesperrt

        try:
            ergebnis = self._inner.complete(
                messages=messages, system=system, tools=tools, max_tokens=max_tokens,
            )
        except LLMError as e:
            self.breaker.melde_fehler(e)
            raise
        except Exception as e:                       # nicht eingeordneter Fehler
            from src.llm.errors import zu_llm_fehler
            fehler = zu_llm_fehler(e)
            self.breaker.melde_fehler(fehler)
            raise fehler from e

        self.breaker.melde_erfolg()
        return ergebnis
