"""Store-Fundament: Sperren, Transaktionsgrenzen, mandate_id.

Beide Threads (Scraping und Telegram) teilen sich EINE Verbindung. Bei einer
geteilten Verbindung beendet ein commit() auch die Transaktion des anderen
Threads — ein halb fertiger Vorgang wäre dann vorzeitig festgeschrieben.
Deshalb ist jede Arbeitseinheit gekapselt.
"""
import ast
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from src.models import Listing
from src.store import Store


def _listing(lid, portal="is24") -> Listing:
    return Listing(id=lid, portal=portal, url="u", titel="T", stadt="Berlin",
                   stadtteil="Charlottenburg", warmmiete=1200.0, flaeche=70.0, zimmer=2.0)


# --- mandate_id -------------------------------------------------------------

def test_mandate_id_wird_gespeichert():
    """Wurde bisher als Parameter entgegengenommen und stillschweigend verworfen —
    fuer die Wiedervorlage wird er gebraucht."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        l = _listing("m1")
        store.claim(l)
        store.save_evaluation(l.id, l.portal, 42, {"passt": True, "score": 80})

        row = store._read("SELECT mandate_id FROM match_log")[0]
        assert row["mandate_id"] == 42
        store.close()


def test_mandate_id_auch_in_der_warteschlange():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.enqueue_eval("x", "is24", 99, "Fehler")
        assert store.get_eval_queue_entry("x", "is24")["mandate_id"] == 99
        store.close()


def test_mandate_id_bleibt_bei_folgefehler_erhalten():
    """Ein spaeterer Fehler ohne Auftragsbezug darf die Zuordnung nicht loeschen."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.enqueue_eval("x", "is24", 99, "erster Fehler")
        store.enqueue_eval("x", "is24", None, "zweiter Fehler")
        assert store.get_eval_queue_entry("x", "is24")["mandate_id"] == 99
        store.close()


# --- Transaktionsgrenzen ----------------------------------------------------

def test_fehler_in_der_transaktion_wird_zurueckgerollt():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.claim(_listing("a"))

        with pytest.raises(sqlite3.IntegrityError):
            with store._tx() as c:
                c.execute("INSERT INTO match_log (listing_id, portal, logged_at, bestanden) "
                          "VALUES ('a','is24','jetzt',1)")
                # NOT NULL auf logged_at verletzen -> die ganze Einheit faellt weg
                c.execute("INSERT INTO match_log (listing_id, portal, logged_at, bestanden) "
                          "VALUES ('a','is24',NULL,1)")

        assert store._read("SELECT * FROM match_log") == [], \
            "auch die erste Anweisung der Einheit darf nicht stehen bleiben"
        store.close()


def test_transaktion_ist_nach_fehler_wieder_nutzbar():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        try:
            with store._tx() as c:
                c.execute("SELECT * FROM gibtesnicht")
        except sqlite3.OperationalError:
            pass
        store.claim(_listing("danach"))
        assert store.is_known("danach", "is24")
        store.close()


# --- Nebenläufigkeit --------------------------------------------------------

def test_zwei_threads_stoeren_sich_nicht():
    """Scraping- und Telegram-Thread arbeiten gleichzeitig auf einer Verbindung."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        fehler = []
        reserviert = []

        def schreiber(praefix):
            try:
                for i in range(60):
                    l = _listing(f"{praefix}-{i}")
                    if store.claim(l):
                        reserviert.append(l.id)
                        store.save_evaluation(l.id, l.portal, 1, {"passt": True, "score": 70})
                        store.mark_notified(l.id, l.portal)
            except Exception as e:      # pragma: no cover
                fehler.append(e)

        def leser():
            try:
                for _ in range(60):
                    store.get_pending_matches()
                    store.recent_matches(5)
                    store.get_any_active_mandate()
                    store.eval_queue_groesse()
            except Exception as e:      # pragma: no cover
                fehler.append(e)

        threads = [threading.Thread(target=schreiber, args=("a",)),
                   threading.Thread(target=schreiber, args=("b",)),
                   threading.Thread(target=leser)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert fehler == [], f"Nebenlaeufigkeitsfehler: {fehler}"
        assert len(reserviert) == 120
        assert len(store._read("SELECT * FROM listings")) == 120
        store.close()


def test_dasselbe_inserat_wird_nur_einmal_reserviert():
    """claim() muss auch unter Gleichzeitigkeit genau einem Thread True geben."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        treffer = []
        start = threading.Barrier(4)

        def versuchen():
            start.wait()
            for i in range(25):
                if store.claim(_listing(f"gleich-{i}")):
                    treffer.append(i)

        threads = [threading.Thread(target=versuchen) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(treffer) == list(range(25)), \
            f"jedes Inserat genau einmal, war: {sorted(treffer)}"
        store.close()


# --- Sperrdisziplin im Quelltext -------------------------------------------

# Diese Funktionen duerfen ohne with-Block auf die Verbindung zugreifen:
# __init__/_setup_pragmas laufen, bevor es einen zweiten Thread gibt; _migrate
# haelt die Sperre selbst; die _migrate_v*/_spalten-Helfer werden ausschliesslich
# von dort aufgerufen.
_OHNE_EIGENE_SPERRE = {
    "__init__", "_setup_pragmas", "_migrate", "_spalten",
    "_migrate_v1", "_migrate_v2", "_migrate_v3", "_insert_listing",
}


def test_kein_verbindungszugriff_ohne_sperre():
    baum = ast.parse(Path("src/store.py").read_text())
    klasse = next(k for k in baum.body
                  if isinstance(k, ast.ClassDef) and k.name == "Store")

    verstoesse = []
    for fn in klasse.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _OHNE_EIGENE_SPERRE:
            continue
        nutzt_conn = any(
            isinstance(n, ast.Attribute) and n.attr == "_conn"
            for n in ast.walk(fn)
        )
        if not nutzt_conn:
            continue
        geschuetzt = any(
            "_lock" in ast.dump(w) or "_tx" in ast.dump(w)
            for w in ast.walk(fn) if isinstance(w, ast.With)
        )
        if not geschuetzt:
            verstoesse.append(fn.name)

    assert verstoesse == [], (
        f"Diese Methoden greifen ungeschuetzt auf die Verbindung zu: {verstoesse}"
    )


def test_nur_die_transaktion_committet():
    """Ausserhalb von _tx darf nichts committen — sonst wuerde ein Aufruf die
    offene Transaktion des anderen Threads mit festschreiben."""
    quelltext = Path("src/store.py").read_text()
    zeilen = [z.strip() for z in quelltext.splitlines() if ".commit()" in z]
    # Erlaubt: der Commit in _tx selbst und der in _migrate (haelt die Sperre).
    assert len(zeilen) == 2, f"unerwartete Commit-Stellen: {zeilen}"


def test_agent_und_main_greifen_nicht_direkt_auf_die_verbindung_zu():
    for datei in ("src/agent.py", "src/main.py", "src/telegram_handler.py"):
        assert "_conn" not in Path(datei).read_text(), \
            f"{datei} umgeht die Sperre"
