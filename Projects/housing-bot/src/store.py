"""SQLite-Speicher: Deduplizierung + vollständiger Listing-Audit-Trail.

Schema-Version 2:
  listings     — alle jemals gesehenen Inserate mit vollständigen Daten
  match_log    — Match/Reject-Entscheidung pro Inserat mit Begründung
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

if TYPE_CHECKING:
    from src.models import Listing, MatchResult

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "housing_bot.db"

# Migration-fähiges Schema: neue Tabellen neben alter seen_listings
_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    id           TEXT NOT NULL,
    portal       TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    notified_at  TEXT,
    PRIMARY KEY (id, portal)
);

CREATE TABLE IF NOT EXISTS listings (
    id           TEXT NOT NULL,
    portal       TEXT NOT NULL,
    url          TEXT,
    titel        TEXT,
    stadt        TEXT,
    stadtteil    TEXT,
    kaltmiete    REAL,
    warmmiete    REAL,
    flaeche      REAL,
    zimmer       REAL,
    merkmale     TEXT,   -- JSON-Array
    seen_at      TEXT NOT NULL,
    notified_at  TEXT,
    PRIMARY KEY (id, portal)
);

CREATE TABLE IF NOT EXISTS match_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id      TEXT NOT NULL,
    portal          TEXT NOT NULL,
    logged_at       TEXT NOT NULL,
    bestanden       INTEGER NOT NULL,   -- 1 = Match, 0 = Reject
    score           INTEGER,
    ablehnungsgrund TEXT,
    geo_ok          INTEGER             -- 1 = im Zielgebiet
);

CREATE INDEX IF NOT EXISTS idx_match_log_listing ON match_log(listing_id, portal);
CREATE INDEX IF NOT EXISTS idx_listings_stadtteil ON listings(stadtteil);
CREATE INDEX IF NOT EXISTS idx_listings_notified  ON listings(notified_at);

CREATE TABLE IF NOT EXISTS mandates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    structured   TEXT,        -- JSON: parsed mandate fields
    state        TEXT NOT NULL DEFAULT 'active',  -- active|paused|stopped
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mandates_state ON mandates(state);

CREATE TABLE IF NOT EXISTS chat_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    role       TEXT NOT NULL,    -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_history_chat ON chat_history(chat_id, id);
"""


class Store:
    def __init__(self, db_path: Union[Path, str] = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            for stmt in _SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._conn.execute(stmt)
            self._conn.commit()

    # --- Deduplizierung (rückwärtskompatibel) ---

    def is_known(self, listing_id: str, portal: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM listings WHERE id = ? AND portal = ?",
            (listing_id, portal),
        ).fetchone()
        if row:
            return True
        # Fallback: alte Tabelle (Migration)
        row = self._conn.execute(
            "SELECT 1 FROM seen_listings WHERE id = ? AND portal = ?",
            (listing_id, portal),
        ).fetchone()
        return row is not None

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def save_listing(self, listing: "Listing") -> None:
        """Speichert vollständige Listing-Daten beim ersten Sehen."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO listings
              (id, portal, url, titel, stadt, stadtteil,
               kaltmiete, warmmiete, flaeche, zimmer, merkmale, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.id,
                listing.portal,
                listing.url,
                listing.titel,
                listing.stadt,
                listing.stadtteil,
                listing.kaltmiete,
                listing.warmmiete,
                listing.flaeche,
                listing.zimmer,
                json.dumps(listing.merkmale, ensure_ascii=False),
                datetime.now().isoformat(),
            ),
        )
        # Rückwärtskompatibilität: auch seen_listings befüllen
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_listings (id, portal, seen_at) VALUES (?, ?, ?)",
            (listing.id, listing.portal, datetime.now().isoformat()),
        )
        self._conn.commit()

    def save_match(self, result: "MatchResult", geo_ok: bool = True) -> None:
        """Speichert das Match/Reject-Ergebnis für einen Audit-Trail."""
        self._conn.execute(
            """
            INSERT INTO match_log
              (listing_id, portal, logged_at, bestanden, score, ablehnungsgrund, geo_ok)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.listing.id,
                result.listing.portal,
                datetime.now().isoformat(),
                1 if result.bestanden else 0,
                result.score,
                result.ablehnungsgrund,
                1 if geo_ok else 0,
            ),
        )
        self._conn.commit()

    def mark_notified(self, listing_id: str, portal: str) -> None:
        ts = datetime.now().isoformat()
        self._conn.execute(
            "UPDATE listings SET notified_at = ? WHERE id = ? AND portal = ?",
            (ts, listing_id, portal),
        )
        self._conn.execute(
            "UPDATE seen_listings SET notified_at = ? WHERE id = ? AND portal = ?",
            (ts, listing_id, portal),
        )
        self._conn.commit()

    # --- Abfragen für spätere Status-Funktion ---

    def recent_matches(self, limit: int = 20) -> List[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT l.*, m.score, m.logged_at as matched_at
            FROM listings l
            JOIN match_log m ON l.id = m.listing_id AND l.portal = m.portal
            WHERE m.bestanden = 1
            ORDER BY m.logged_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def recent_rejects(self, limit: int = 20) -> List[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT l.titel, l.stadtteil, l.warmmiete, m.ablehnungsgrund, m.logged_at
            FROM listings l
            JOIN match_log m ON l.id = m.listing_id AND l.portal = m.portal
            WHERE m.bestanden = 0
            ORDER BY m.logged_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    # --- Mandate ---

    def save_mandate(self, chat_id: str, raw_text: str, structured: dict) -> int:
        """Speichert einen neuen Suchauftrag, deaktiviert alte."""
        now = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE mandates SET state = 'stopped', updated_at = ? WHERE chat_id = ? AND state IN ('active','paused')",
                (now, chat_id),
            )
            cur = self._conn.execute(
                """INSERT INTO mandates (chat_id, raw_text, structured, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (chat_id, raw_text, json.dumps(structured, ensure_ascii=False), now, now),
            )
            self._conn.commit()
        return cur.lastrowid

    def get_active_mandate(self, chat_id: str) -> Optional[dict]:
        """Gibt den aktiven Auftrag zurück oder None."""
        row = self._conn.execute(
            "SELECT * FROM mandates WHERE chat_id = ? AND state = 'active' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["structured"] = json.loads(d["structured"]) if d.get("structured") else {}
        return d

    def get_any_active_mandate(self) -> Optional[dict]:
        """Gibt irgendeinen aktiven Auftrag zurück (für den Polling-Loop)."""
        row = self._conn.execute(
            "SELECT * FROM mandates WHERE state = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["structured"] = json.loads(d["structured"]) if d.get("structured") else {}
        return d

    def set_mandate_state(self, chat_id: str, state: str) -> bool:
        """Setzt den Zustand des aktiven Auftrags."""
        now = datetime.now().isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE mandates SET state = ?, updated_at = ? WHERE chat_id = ? AND state IN ('active','paused')",
                (state, now, chat_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # --- Evaluations-Log ---

    def save_evaluation(self, listing_id: str, portal: str, mandate_id: int, evaluation: dict) -> None:
        """Speichert das KI-Bewertungsergebnis."""
        self._conn.execute(
            """INSERT OR REPLACE INTO match_log
               (listing_id, portal, logged_at, bestanden, score, ablehnungsgrund, geo_ok)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                listing_id,
                portal,
                datetime.now().isoformat(),
                1 if evaluation.get("passt") else 0,
                evaluation.get("score", 0),
                evaluation.get("kurzfazit", ""),
            ),
        )
        self._conn.commit()

    # --- Chat-Verlauf (überlebt Neustarts) ---

    def append_chat(self, chat_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, datetime.now().isoformat()),
            )
            self._conn.commit()

    def get_chat_history(self, chat_id: str, limit: int = 24) -> List[dict]:
        rows = self._conn.execute(
            "SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        # chronologisch zurückgeben (älteste zuerst)
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def close(self) -> None:
        self._conn.close()
