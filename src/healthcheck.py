"""Gesundheitsprüfung für Docker: ``python -m src.healthcheck``.

Rückgabewert 0 = healthy oder degraded, 1 = unhealthy.

Degraded bleibt bewusst 0: Ein gestörter Modellanbieter oder eine angestaute
Warteschlange sind Gründe hinzusehen, aber kein Grund, den Container
neuzustarten — der Bot nimmt weiter Nachrichten an und puffert die Arbeit.
Ein Neustart würde die Störung nicht beheben, aber den Verlauf unterbrechen.

Es wird kein Modellaufruf ausgelöst.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Optional

from src.health import DEGRADED, HEALTHY, UNHEALTHY, bewerten, lesen
from src.store import DEFAULT_DB_PATH


def datenbank_pruefen(pfad: Optional[Path] = None) -> tuple:
    """Nur lesend und ohne Migration — der Check darf nichts verändern."""
    # Zur Aufrufzeit auflösen statt als Standardwert einzufrieren: Ein
    # Standardwert wird einmal beim Import gebunden und wäre danach fest.
    pfad = pfad if pfad is not None else DEFAULT_DB_PATH
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % pfad, uri=True, timeout=3)
        try:
            conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        finally:
            conn.close()
        return True, ""
    except sqlite3.Error as e:
        return False, str(e)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ausfuehrlich = "-v" in argv or "--verbose" in argv

    db_ok, db_fehler = datenbank_pruefen()
    daten = lesen()
    status, gruende = bewerten(daten, db_ok=db_ok)

    if not db_ok and db_fehler:
        gruende = list(gruende) + ["DB: %s" % db_fehler[:100]]

    zeile = status.upper()
    if gruende:
        zeile += " — " + "; ".join(gruende)
    print(zeile)

    if ausfuehrlich and daten:
        for feld in ("gestartet_am", "aktualisiert_am", "zyklus", "llm", "queue"):
            print("  %-14s %s" % (feld, daten.get(feld)))

    return 1 if status == UNHEALTHY else 0


if __name__ == "__main__":
    sys.exit(main())
