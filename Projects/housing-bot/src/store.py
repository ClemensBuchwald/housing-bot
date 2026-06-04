"""SQLite-basierter Deduplizierungsspeicher."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Union

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "housing_bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    id      TEXT NOT NULL,
    portal  TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    notified_at TEXT,
    PRIMARY KEY (id, portal)
);
"""


class Store:
    def __init__(self, db_path: Union[Path, str] = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def is_known(self, listing_id: str, portal: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_listings WHERE id = ? AND portal = ?",
            (listing_id, portal),
        ).fetchone()
        return row is not None

    def mark_seen(self, listing_id: str, portal: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_listings (id, portal, seen_at) VALUES (?, ?, ?)",
            (listing_id, portal, datetime.now().isoformat()),
        )
        self._conn.commit()

    def mark_notified(self, listing_id: str, portal: str) -> None:
        self._conn.execute(
            "UPDATE seen_listings SET notified_at = ? WHERE id = ? AND portal = ?",
            (datetime.now().isoformat(), listing_id, portal),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
