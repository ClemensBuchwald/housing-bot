"""SQLite-Speicher: Deduplizierung + vollständiger Listing-Audit-Trail.

Schema-Version 2:
  listings     — alle jemals gesehenen Inserate mit vollständigen Daten
  match_log    — Match/Reject-Entscheidung pro Inserat mit Begründung
"""
from __future__ import annotations

import json
import sqlite3
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
"""


class Store:
    def __init__(self, db_path: Union[Path, str] = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
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

    def close(self) -> None:
        self._conn.close()
