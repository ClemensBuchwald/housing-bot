"""Technischer Zustand des Bots — geschrieben vom Prozess, gelesen von aussen.

Bisher prüfte Docker nur, ob ``/app/data`` anlegbar ist. Das war selbst dann
noch „gesund", wenn der Scraping-Thread längst gestorben war und seit Stunden
niemand mehr etwas suchte.

Die Antwort darauf ist eine kleine Zustandsdatei neben der Datenbank. Der
Prozess schreibt hinein, ``src.healthcheck`` und der Telegram-Befehl
``/zustand`` lesen daraus. Bewusst eine Datei und kein Netzdienst: Es gibt
nichts zu betreiben, nichts zu sichern und nichts, das selbst ausfallen kann.

Für einen Gesundheitscheck wird nie das Modell angerufen — das wäre teuer,
langsam und würde ausgerechnet bei einem Anbieterausfall zusätzlich belasten.
Stattdessen meldet der Stromkreis (siehe llm/circuit.py), was er ohnehin weiss.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_HEALTH_PATH = Path(__file__).parent.parent / "data" / "health.json"

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"

# Wie alt ein Lebenszeichen höchstens sein darf, je Thread.
# Scraping grosszügig: Ein voller Zyklus mit trägen Quellen und Bewertungen
# dauert mehrere Minuten, und dazwischen liegt die Wartezeit bis zum nächsten.
HERZSCHLAG_MAX_S = {"scraping": 1800, "telegram": 180}

# Ab so vielen offenen Bewertungen gilt die Warteschlange als angestaut.
QUEUE_STAU_SCHWELLE = 50
# So viele erfolglose Telegram-Abrufe hintereinander gelten als Störung.
TELEGRAM_FEHLERSERIE_SCHWELLE = 5


def _jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Health:
    """Sammelt den Zustand im Speicher und schreibt ihn gedrosselt auf Platte."""

    def __init__(self, pfad: Optional[Path] = None, schreib_intervall_s: float = 5.0) -> None:
        self.pfad = Path(pfad) if pfad else DEFAULT_HEALTH_PATH
        self._lock = threading.Lock()
        self._intervall = schreib_intervall_s
        self._zuletzt_geschrieben = 0.0
        self._zustand: Dict[str, Any] = {
            "gestartet_am": _jetzt(),
            "pid": os.getpid(),
            "threads": {},
            "zyklus": None,
            "llm": {"zustand": "geschlossen"},
            "queue": {},
            "telegram": {"letzter_erfolg": None, "letzter_fehler": None, "fehlerserie": 0},
            "db": {"ok": True, "letzter_fehler": None},
        }

    # --- Schreiben --------------------------------------------------------

    def herzschlag(self, thread: str) -> None:
        """Lebenszeichen eines Threads. Billig — darf oft aufgerufen werden."""
        with self._lock:
            self._zustand["threads"][thread] = {
                "zeit": _jetzt(),
                "zeitstempel": time.time(),
                "max_alter_s": HERZSCHLAG_MAX_S.get(thread, 600),
            }
        self.schreiben()

    def zyklus_beendet(self, quellen: int, quellen_fehler: int,
                       inserate: int, treffer: int, dauer_s: float) -> None:
        with self._lock:
            self._zustand["zyklus"] = {
                "beendet_am": _jetzt(),
                "zeitstempel": time.time(),
                "quellen": quellen,
                "quellen_fehler": quellen_fehler,
                "inserate": inserate,
                "treffer": treffer,
                "dauer_s": round(dauer_s, 1),
            }
        self.schreiben(erzwingen=True)

    def llm_zustand(self, info: Optional[dict]) -> None:
        with self._lock:
            self._zustand["llm"] = info or {"zustand": "unbekannt"}

    def queue_zustand(self, zaehler: dict) -> None:
        with self._lock:
            self._zustand["queue"] = zaehler

    def telegram_erfolg(self) -> None:
        with self._lock:
            self._zustand["telegram"]["letzter_erfolg"] = _jetzt()
            self._zustand["telegram"]["fehlerserie"] = 0

    def telegram_fehler(self, code: Optional[int], hinweis: str) -> None:
        """Ein erfolgloser Abruf. Der Text darf keinen Token enthalten —
        die Aufrufstelle gibt nur Statuscode und Kurzbeschreibung weiter."""
        with self._lock:
            t = self._zustand["telegram"]
            t["fehlerserie"] = int(t.get("fehlerserie", 0)) + 1
            t["letzter_fehler"] = {"code": code, "hinweis": hinweis[:120], "wann": _jetzt()}
        self.schreiben(erzwingen=True)

    def db_fehler(self, hinweis: str) -> None:
        with self._lock:
            self._zustand["db"] = {"ok": False, "letzter_fehler": hinweis[:200],
                                   "wann": _jetzt()}
        self.schreiben(erzwingen=True)

    def db_ok(self) -> None:
        with self._lock:
            if not self._zustand["db"].get("ok"):
                self._zustand["db"] = {"ok": True, "letzter_fehler": None}

    def schreiben(self, erzwingen: bool = False) -> None:
        """Atomar über eine temporäre Datei — ein halb geschriebener Zustand
        wäre schlimmer als ein etwas veralteter."""
        jetzt = time.time()
        with self._lock:
            if not erzwingen and (jetzt - self._zuletzt_geschrieben) < self._intervall:
                return
            self._zuletzt_geschrieben = jetzt
            daten = dict(self._zustand)
            daten["aktualisiert_am"] = _jetzt()
            daten["aktualisiert_zeitstempel"] = jetzt
        try:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pfad.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2))
            os.replace(str(tmp), str(self.pfad))
        except OSError as e:
            logger.warning("Zustandsdatei nicht schreibbar: %s", e)

    def schnappschuss(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._zustand, default=str))


# --- Auswertung (auch ohne laufenden Prozess nutzbar) ----------------------

def lesen(pfad: Optional[Path] = None) -> Optional[dict]:
    p = Path(pfad) if pfad else DEFAULT_HEALTH_PATH
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def bewerten(daten: Optional[dict], db_ok: bool = True,
             jetzt: Optional[float] = None) -> tuple:
    """Gesamturteil aus einem Zustandsdatensatz.

    Rückgabe: (status, gruende) — gruende ist eine Liste kurzer Klartexte.
    """
    jetzt = jetzt if jetzt is not None else time.time()
    schwer: list = []
    leicht: list = []

    if not db_ok:
        schwer.append("Datenbank nicht verwendbar")
    if daten is None:
        schwer.append("keine Zustandsdatei")
        return UNHEALTHY, schwer

    if not daten.get("db", {}).get("ok", True):
        schwer.append("Datenbankfehler im Betrieb")

    threads = daten.get("threads") or {}
    if not threads:
        schwer.append("kein Thread hat je ein Lebenszeichen gegeben")
    for name, eintrag in threads.items():
        stempel = eintrag.get("zeitstempel")
        grenze = eintrag.get("max_alter_s", 600)
        if stempel is None:
            schwer.append(f"Thread {name} ohne Zeitstempel")
            continue
        alter = jetzt - float(stempel)
        if alter > grenze:
            schwer.append(f"Thread {name} seit {int(alter)}s ohne Lebenszeichen")

    for erwartet in ("scraping", "telegram"):
        if erwartet not in threads:
            schwer.append(f"Thread {erwartet} fehlt")

    llm = daten.get("llm") or {}
    if llm.get("zustand") in ("offen", "halb_offen"):
        leicht.append("LLM-Stromkreis %s (%s)" % (llm.get("zustand"),
                                                  llm.get("kategorie") or "unbekannt"))

    queue = daten.get("queue") or {}
    offen = int(queue.get("pending", 0)) + int(queue.get("blocked", 0))
    if offen >= QUEUE_STAU_SCHWELLE:
        leicht.append(f"{offen} Bewertungen angestaut")

    tg = daten.get("telegram") or {}
    if int(tg.get("fehlerserie", 0)) >= TELEGRAM_FEHLERSERIE_SCHWELLE:
        code = (tg.get("letzter_fehler") or {}).get("code")
        leicht.append(f"Telegram antwortet nicht (zuletzt {code})")

    if schwer:
        return UNHEALTHY, schwer
    if leicht:
        return DEGRADED, leicht
    return HEALTHY, []
