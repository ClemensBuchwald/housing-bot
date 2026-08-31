"""DB-Migration: additiv, idempotent, ohne Rückwirkung auf Bestandsdaten.

Die Warteschlange startet ausdrücklich LEER. Würde sie aus fehlenden
match_log-Einträgen abgeleitet, wäre nach dem ersten Start schlagartig die
gesamte Historie wieder bewertbar — und meldbar.
"""
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.store import SCHEMA_VERSION, Store

# Schema, wie es vor der Versionierung auf dem Server lag (user_version = 0).
_ALTES_SCHEMA = """
CREATE TABLE seen_listings (
    id TEXT NOT NULL, portal TEXT NOT NULL, seen_at TEXT NOT NULL,
    notified_at TEXT, PRIMARY KEY (id, portal));
CREATE TABLE listings (
    id TEXT NOT NULL, portal TEXT NOT NULL, url TEXT, titel TEXT, stadt TEXT,
    stadtteil TEXT, kaltmiete REAL, warmmiete REAL, flaeche REAL, zimmer REAL,
    merkmale TEXT, seen_at TEXT NOT NULL, notified_at TEXT,
    PRIMARY KEY (id, portal));
CREATE TABLE match_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL,
    portal TEXT NOT NULL, logged_at TEXT NOT NULL, bestanden INTEGER NOT NULL,
    score INTEGER, ablehnungsgrund TEXT, geo_ok INTEGER, evaluation TEXT);
CREATE TABLE mandates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
    raw_text TEXT NOT NULL, structured TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def _bestands_db(pfad: Path) -> Path:
    """Legt eine Datenbank im Vor-Migrations-Zustand mit echten Daten an."""
    c = sqlite3.connect(str(pfad))
    c.executescript(_ALTES_SCHEMA)
    c.execute("INSERT INTO listings (id, portal, url, titel, stadt, stadtteil, "
              "kaltmiete, warmmiete, flaeche, zimmer, merkmale, seen_at, notified_at) "
              "VALUES ('alt1','is24','u','Altbau','Berlin','Charlottenburg',"
              "1000,1300,80,3,'[]','2026-08-01T10:00:00','2026-08-01T11:00:00')")
    c.execute("INSERT INTO listings (id, portal, url, titel, stadt, stadtteil, "
              "kaltmiete, warmmiete, flaeche, zimmer, merkmale, seen_at) "
              "VALUES ('alt2','immowelt','u2','Neubau','Berlin','Wilmersdorf',"
              "1100,1400,90,3,'[]','2026-08-02T10:00:00')")
    c.execute("INSERT INTO seen_listings (id, portal, seen_at) VALUES ('alt1','is24','2026-08-01T10:00:00')")
    c.execute("INSERT INTO match_log (listing_id, portal, logged_at, bestanden, score, "
              "ablehnungsgrund, geo_ok, evaluation) VALUES "
              "('alt1','is24','2026-08-01T10:05:00',1,88,'passt',1,'{\"passt\": true}')")
    c.execute("INSERT INTO match_log (listing_id, portal, logged_at, bestanden, score, "
              "ablehnungsgrund, geo_ok) VALUES ('alt2','immowelt','2026-08-02T10:05:00',0,10,'zu teuer',1)")
    c.execute("INSERT INTO mandates (chat_id, raw_text, structured, state, created_at, updated_at) "
              "VALUES ('chat1','3 Zimmer CW','{\"zimmer_min\": 3}','active','2026-08-01T09:00:00','2026-08-01T09:00:00')")
    c.execute("INSERT INTO chat_history (chat_id, role, content, created_at) "
              "VALUES ('chat1','user','Hallo','2026-08-01T09:00:00')")
    c.commit()
    c.close()
    return pfad


def _inhalt(pfad: Path, tabelle: str, spalten: str = "*") -> str:
    """Stabiler Fingerabdruck des Tabelleninhalts."""
    c = sqlite3.connect(str(pfad))
    c.text_factory = str
    rows = c.execute("SELECT %s FROM %s ORDER BY rowid" % (spalten, tabelle)).fetchall()
    c.close()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


# --- Fall 5: eval_queue startet leer ---------------------------------------

def test_eval_queue_nach_migration_leer():
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")
        store = Store(pfad)
        assert store.eval_queue_groesse() == 0, (
            "Kein Backfill: Die Warteschlange darf nach der Migration keinen "
            "einzigen Altfall enthalten"
        )
        assert store.get_due_evaluations(jetzt="2999-01-01T00:00:00") == []
        store.close()


def test_kein_backfill_aus_fehlenden_match_log_eintraegen():
    """'alt2' hat eine Ablehnung, ein drittes Inserat gar keinen Eintrag —
    beides darf keine offene Bewertung erzeugen."""
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")
        c = sqlite3.connect(str(pfad))
        c.execute("INSERT INTO listings (id, portal, url, titel, stadt, merkmale, seen_at) "
                  "VALUES ('ohne-bewertung','is24','u','X','Berlin','[]','2026-08-03T10:00:00')")
        c.commit(); c.close()

        store = Store(pfad)
        assert store.eval_queue_groesse() == 0
        store.close()


def test_historische_inserate_werden_nicht_erneut_gemeldet():
    """'alt1' ist bereits gemeldet, 'alt2' wurde abgelehnt — nach der Migration
    darf keines von beiden als offener Treffer auftauchen."""
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")
        store = Store(pfad)
        assert store.get_pending_matches() == []
        store.close()


# --- Fall 6: Bestandsdaten bleiben unveraendert ----------------------------

def test_bestandsdaten_bleiben_unveraendert():
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")

        vorher = {
            "listings": _inhalt(pfad, "listings"),
            "mandates": _inhalt(pfad, "mandates"),
            "chat_history": _inhalt(pfad, "chat_history"),
            "seen_listings": _inhalt(pfad, "seen_listings"),
            # match_log bekommt eine neue Spalte -> die alten Spalten vergleichen
            "match_log": _inhalt(pfad, "match_log",
                                 "id, listing_id, portal, logged_at, bestanden, "
                                 "score, ablehnungsgrund, geo_ok, evaluation"),
        }

        store = Store(pfad)
        store.close()

        nachher = {
            "listings": _inhalt(pfad, "listings"),
            "mandates": _inhalt(pfad, "mandates"),
            "chat_history": _inhalt(pfad, "chat_history"),
            "seen_listings": _inhalt(pfad, "seen_listings"),
            "match_log": _inhalt(pfad, "match_log",
                                 "id, listing_id, portal, logged_at, bestanden, "
                                 "score, ablehnungsgrund, geo_ok, evaluation"),
        }
        assert vorher == nachher, "Die Migration darf keine Bestandsdaten anfassen"


def test_migration_ergaenzt_nur_additiv():
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")
        store = Store(pfad)

        spalten = {r[1] for r in store._conn.execute("PRAGMA table_info(match_log)")}
        assert "mandate_id" in spalten, "neue Spalte muss da sein"
        assert {"id", "listing_id", "portal", "logged_at", "bestanden", "score",
                "ablehnungsgrund", "geo_ok", "evaluation"} <= spalten, \
            "keine alte Spalte darf verschwinden"

        tabellen = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"listings", "seen_listings", "match_log", "mandates",
                "chat_history", "eval_queue"} <= tabellen
        store.close()


def test_alter_auftrag_bleibt_aktiv():
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")
        store = Store(pfad)
        m = store.get_active_mandate("chat1")
        assert m is not None and m["raw_text"] == "3 Zimmer CW"
        assert m["structured"] == {"zimmer_min": 3}
        store.close()


# --- Fall 13: Idempotenz ---------------------------------------------------

def test_migration_ist_idempotent():
    with tempfile.TemporaryDirectory() as d:
        pfad = _bestands_db(Path(d) / "bestand.db")

        Store(pfad).close()
        stand1 = {t: _inhalt(pfad, t) for t in
                  ("listings", "mandates", "chat_history", "seen_listings", "eval_queue")}
        schema1 = _inhalt(pfad, "sqlite_master", "type, name, sql")

        for _ in range(3):
            s = Store(pfad)
            assert s.schema_version() == SCHEMA_VERSION
            s.close()

        stand2 = {t: _inhalt(pfad, t) for t in
                  ("listings", "mandates", "chat_history", "seen_listings", "eval_queue")}
        schema2 = _inhalt(pfad, "sqlite_master", "type, name, sql")

        assert stand1 == stand2, "wiederholte Migration darf nichts veraendern"
        assert schema1 == schema2, "auch das Schema muss stabil bleiben"


def test_frische_datenbank_bekommt_volle_version():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "neu.db")
        assert store.schema_version() == SCHEMA_VERSION
        assert store.eval_queue_groesse() == 0
        store.close()


def test_wal_und_busy_timeout_aktiv():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "neu.db")
        modus = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(modus).lower() == "wal"
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        store.close()


# --- Fall 14: echter Serverbestand -----------------------------------------

def _snapshot_pfad():
    p = os.getenv("HOUSING_BOT_PROD_SNAPSHOT")
    return Path(p) if p and Path(p).is_file() else None


@pytest.mark.skipif(_snapshot_pfad() is None,
                    reason="Kein Produktions-Schnappschuss gesetzt "
                           "(HOUSING_BOT_PROD_SNAPSHOT). Enthaelt echte Chatdaten "
                           "und gehoert deshalb nicht ins Repository.")
def test_migration_auf_kopie_des_serverbestands():
    """Läuft gegen eine Kopie der laufenden Produktionsdatenbank.

    Die Kopie entsteht über die SQLite-Backup-Schnittstelle; die Produktions-DB
    selbst wird dabei nur gelesen und hier nie verändert.
    """
    quelle = _snapshot_pfad()
    with tempfile.TemporaryDirectory() as d:
        pfad = Path(d) / "prod-kopie.db"
        shutil.copy(quelle, pfad)

        c = sqlite3.connect(str(pfad))
        vorher = {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                  for t in ("listings", "match_log", "mandates", "chat_history", "seen_listings")}
        version_vorher = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        assert vorher["listings"] > 0, "Schnappschuss wirkt leer"

        store = Store(pfad)

        assert version_vorher == 0, "Serverbestand ist noch unversioniert"
        assert store.schema_version() == SCHEMA_VERSION

        nachher = {t: len(store._read("SELECT 1 FROM %s" % t))
                   for t in ("listings", "match_log", "mandates", "chat_history", "seen_listings")}
        assert nachher == vorher, "kein Datensatz darf hinzukommen oder verschwinden"

        assert store.eval_queue_groesse() == 0, \
            "die gesamte Historie darf nicht als offene Bewertung auftauchen"
        assert store.get_pending_matches() == [], \
            "kein historisches Inserat darf ploetzlich meldebereit werden"

        # Zweiter Lauf auf derselben Datei: nichts aendert sich mehr.
        store.close()
        store2 = Store(pfad)
        assert store2.schema_version() == SCHEMA_VERSION
        assert store2.eval_queue_groesse() == 0
        nochmal = {t: len(store2._read("SELECT 1 FROM %s" % t))
                   for t in ("listings", "match_log", "mandates", "chat_history", "seen_listings")}
        assert nochmal == vorher
        store2.close()
