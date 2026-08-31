"""SQLite-Speicher: Deduplizierung, Listing-Audit-Trail und Bewertungs-Warteschlange.

Nebenläufigkeit
---------------
Genau EINE Verbindung, geteilt von Scraping- und Telegram-Thread
(``check_same_thread=False``). Jede Arbeitseinheit läuft deshalb unter
``_tx()`` bzw. ``_read()`` — beide halten ``self._lock``.

Der Grund ist nicht nur Datenwettlauf: Bei einer geteilten Verbindung beendet
``commit()`` die Transaktion des *anderen* Threads gleich mit. Ein halb fertiger
Vorgang wäre dann vorzeitig festgeschrieben. ``_tx()`` klammert deshalb
Schreiben und Commit zu einer Einheit — außerhalb von ``_tx()`` wird nirgends
committet.

Schema-Versionen (PRAGMA user_version)
--------------------------------------
  1  Basis: seen_listings, listings, match_log (+evaluation), mandates, chat_history
  2  match_log.mandate_id — welcher Auftrag hat diese Bewertung veranlasst
  3  eval_queue — Inserate, deren Bewertung technisch scheiterte

Die Migration ist additiv und idempotent: Bestandsdatenbanken (user_version = 0,
Tabellen bereits vorhanden) durchlaufen Stufe 1 als reine No-ops.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, List, Optional, Union

if TYPE_CHECKING:
    from src.models import Listing, MatchResult

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "housing_bot.db"

SCHEMA_VERSION = 3

# Wartezeit, bevor SQLite bei belegter Datenbank aufgibt. Betrifft vor allem den
# Moment, in dem der Scraping-Thread schreibt und der Telegram-Thread lesen will.
BUSY_TIMEOUT_MS = 5000

_V1_SCHEMA = """
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

# Zustände eines Warteschlangen-Eintrags.
EVAL_PENDING = "pending"    # wartet auf erneute Bewertung
EVAL_BLOCKED = "blocked"    # nicht von selbst heilbar (z. B. Auth) — wartet auf Eingriff
EVAL_FAILED = "failed"      # Versuche aufgebraucht
EVAL_EXPIRED = "expired"    # zu alt — eine Wohnung von gestern hilft niemandem mehr

_V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_queue (
    listing_id    TEXT NOT NULL,
    portal        TEXT NOT NULL,
    mandate_id    INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    retry_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (listing_id, portal)
);

CREATE INDEX IF NOT EXISTS idx_eval_queue_faellig ON eval_queue(status, next_retry_at);
"""


def _jetzt() -> str:
    return datetime.now().isoformat()


class Store:
    def __init__(self, db_path: Union[Path, str] = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._setup_pragmas()
        self._migrate()

    # --- Verbindungsparameter -------------------------------------------------

    def _setup_pragmas(self) -> None:
        """Muss vor der ersten Transaktion laufen — journal_mode ist außerhalb
        einer Transaktion zu setzen.

        WAL passt hier: ein Prozess, zwei Threads, lokales Dateisystem (Bind-Mount).
        Leser blockieren den Schreiber nicht mehr, was den Telegram-Thread von den
        langen Scraping-Schreibphasen entkoppelt.
        """
        try:
            modus = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(modus).lower() != "wal":
                logger.warning("WAL nicht aktiv (journal_mode=%s) — Betrieb bleibt möglich", modus)
        except sqlite3.Error as e:
            # Etwa auf Dateisystemen ohne gemeinsamen Speicher. Kein Grund aufzugeben.
            logger.warning("WAL konnte nicht aktiviert werden: %s", e)
        self._conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
        # WAL + NORMAL ist der übliche Kompromiss: Dauerhaftigkeit bleibt bis auf
        # den Stromausfall-Sonderfall erhalten, spart aber ein fsync je Commit.
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # --- Zugriffsprimitive ----------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Eine Schreibeinheit: exklusiv, mit genau einem Commit am Ende.

        Der einzige Ort im Modul, an dem committet wird.
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _read(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _read_one(self, sql: str, params=()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --- Migration ------------------------------------------------------------

    def _migrate(self) -> None:
        """Additiv und idempotent. Löscht nichts und schreibt keine Bestandsdaten um."""
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            try:
                if version < 1:
                    self._migrate_v1()
                if version < 2:
                    self._migrate_v2()
                if version < 3:
                    self._migrate_v3()
                # PRAGMA erlaubt keine Parameterbindung; der Wert ist eine Konstante.
                self._conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if version:
            logger.info("Store-Schema von Version %d auf %d gehoben", version, SCHEMA_VERSION)

    def _spalten(self, tabelle: str) -> set:
        return {r[1] for r in self._conn.execute("PRAGMA table_info(%s)" % tabelle)}

    def _migrate_v1(self) -> None:
        """Basisschema. Auf einer Bestands-DB (user_version 0, Tabellen vorhanden)
        läuft alles als No-op durch — genau dafür sind IF NOT EXISTS und die
        Spaltenprüfung da."""
        for stmt in _V1_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        if "evaluation" not in self._spalten("match_log"):
            self._conn.execute("ALTER TABLE match_log ADD COLUMN evaluation TEXT")

    def _migrate_v2(self) -> None:
        """Welcher Auftrag hat die Bewertung veranlasst? Wurde bisher übergeben,
        aber verworfen — für die Wiedervorlage wird er gebraucht."""
        if "mandate_id" not in self._spalten("match_log"):
            self._conn.execute("ALTER TABLE match_log ADD COLUMN mandate_id INTEGER")

    def _migrate_v3(self) -> None:
        """eval_queue. Startet bewusst LEER: kein Backfill, keine Ableitung
        offener Bewertungen aus fehlenden match_log-Einträgen. Sonst würde die
        gesamte Historie schlagartig wieder bewertbar — und meldbar."""
        for stmt in _V3_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)

    def schema_version(self) -> int:
        row = self._read_one("PRAGMA user_version")
        return int(row[0]) if row else 0

    # --- Deduplizierung (rückwärtskompatibel) ---

    def is_known(self, listing_id: str, portal: str) -> bool:
        with self._lock:
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
        with self._tx() as c:
            self._insert_listing(c, listing)

    def _insert_listing(self, c: sqlite3.Connection, listing: "Listing") -> int:
        """Gemeinsamer Kern von save_listing und claim. Erwartet gehaltenes Lock."""
        ts = _jetzt()
        cur = c.execute(
            """
            INSERT OR IGNORE INTO listings
              (id, portal, url, titel, stadt, stadtteil,
               kaltmiete, warmmiete, flaeche, zimmer, merkmale, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.id, listing.portal, listing.url, listing.titel,
                listing.stadt, listing.stadtteil, listing.kaltmiete,
                listing.warmmiete, listing.flaeche, listing.zimmer,
                json.dumps(listing.merkmale, ensure_ascii=False), ts,
            ),
        )
        # Rückwärtskompatibilität: auch seen_listings befüllen
        c.execute(
            "INSERT OR IGNORE INTO seen_listings (id, portal, seen_at) VALUES (?, ?, ?)",
            (listing.id, listing.portal, ts),
        )
        return cur.rowcount

    def claim(self, listing: "Listing") -> bool:
        """Reserviert ein Inserat atomar. True = wir haben es zuerst gesehen.

        Ersetzt das Muster "is_known() prüfen, später speichern": dazwischen lag
        ein Zeitfenster, in dem der zweite Thread (Sofort-Suche vs. Dauersuche)
        dasselbe Inserat als neu ansah — doppelte KI-Bewertung und Doppel-Alert.
        """
        with self._tx() as c:
            return self._insert_listing(c, listing) > 0

    def update_listing_felder(self, listing: "Listing") -> None:
        """Aktualisiert nach dem Detail-Abruf ergänzte Felder (Warmmiete, Merkmale)."""
        with self._tx() as c:
            c.execute(
                """UPDATE listings SET warmmiete = COALESCE(?, warmmiete),
                                       kaltmiete = COALESCE(?, kaltmiete),
                                       merkmale = ?
                   WHERE id = ? AND portal = ?""",
                (listing.warmmiete, listing.kaltmiete,
                 json.dumps(listing.merkmale, ensure_ascii=False),
                 listing.id, listing.portal),
            )

    def get_pending_matches(self, limit: int = 20) -> List[sqlite3.Row]:
        """Treffer, die bewertet wurden, aber noch NICHT gemeldet sind.

        Entsteht, wenn ein Alert am Limit abgeschnitten wurde oder der
        Telegram-Versand fehlschlug. Diese Treffer dürfen nicht verloren gehen.

        match_log ist ein Verlaufsprotokoll: eine Wiedervorlage nach technischem
        Fehler legt einen ZWEITEN Eintrag zum selben Inserat an. Ohne die
        MAX(id)-Einschränkung stünde das Inserat dann doppelt in der Liste und
        ginge zweimal an den Nutzer. Es zählt immer nur die jüngste Bewertung.
        """
        return self._read(
            """
            SELECT l.*, m.score, m.evaluation
            FROM listings l
            JOIN match_log m ON l.id = m.listing_id AND l.portal = m.portal
            WHERE m.bestanden = 1
              AND l.notified_at IS NULL
              AND m.id = (SELECT MAX(m2.id) FROM match_log m2
                          WHERE m2.listing_id = l.id AND m2.portal = l.portal)
            ORDER BY m.score DESC, m.logged_at ASC
            LIMIT ?
            """,
            (limit,),
        )

    def save_match(self, result: "MatchResult", geo_ok: bool = True) -> None:
        """Speichert das Match/Reject-Ergebnis für einen Audit-Trail."""
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO match_log
                  (listing_id, portal, logged_at, bestanden, score, ablehnungsgrund, geo_ok)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.listing.id, result.listing.portal, _jetzt(),
                    1 if result.bestanden else 0, result.score,
                    result.ablehnungsgrund, 1 if geo_ok else 0,
                ),
            )

    def mark_notified(self, listing_id: str, portal: str) -> None:
        ts = _jetzt()
        with self._tx() as c:
            c.execute(
                "UPDATE listings SET notified_at = ? WHERE id = ? AND portal = ?",
                (ts, listing_id, portal),
            )
            c.execute(
                "UPDATE seen_listings SET notified_at = ? WHERE id = ? AND portal = ?",
                (ts, listing_id, portal),
            )

    def ist_gemeldet(self, listing_id: str, portal: str) -> bool:
        """Wurde dieses Inserat dem Nutzer schon geschickt?"""
        row = self._read_one(
            "SELECT notified_at FROM listings WHERE id = ? AND portal = ?",
            (listing_id, portal),
        )
        return bool(row and row["notified_at"])

    # --- Abfragen für spätere Status-Funktion ---

    def recent_matches(self, limit: int = 20) -> List[sqlite3.Row]:
        return self._read(
            """
            SELECT l.*, m.score, m.logged_at as matched_at
            FROM listings l
            JOIN match_log m ON l.id = m.listing_id AND l.portal = m.portal
            WHERE m.bestanden = 1
            ORDER BY m.logged_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def recent_rejects(self, limit: int = 20) -> List[sqlite3.Row]:
        return self._read(
            """
            SELECT l.titel, l.stadtteil, l.warmmiete, m.ablehnungsgrund, m.logged_at
            FROM listings l
            JOIN match_log m ON l.id = m.listing_id AND l.portal = m.portal
            WHERE m.bestanden = 0
            ORDER BY m.logged_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    # --- Mandate ---

    def save_mandate(self, chat_id: str, raw_text: str, structured: dict) -> int:
        """Speichert einen neuen Suchauftrag, deaktiviert alte."""
        now = _jetzt()
        with self._tx() as c:
            c.execute(
                "UPDATE mandates SET state = 'stopped', updated_at = ? "
                "WHERE chat_id = ? AND state IN ('active','paused')",
                (now, chat_id),
            )
            cur = c.execute(
                """INSERT INTO mandates (chat_id, raw_text, structured, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (chat_id, raw_text, json.dumps(structured, ensure_ascii=False), now, now),
            )
            neue_id = cur.lastrowid
        return neue_id

    def _mandate_dict(self, row: Optional[sqlite3.Row]) -> Optional[dict]:
        if not row:
            return None
        d = dict(row)
        d["structured"] = json.loads(d["structured"]) if d.get("structured") else {}
        return d

    def get_active_mandate(self, chat_id: str) -> Optional[dict]:
        """Gibt den aktiven Auftrag zurück oder None."""
        return self._mandate_dict(self._read_one(
            "SELECT * FROM mandates WHERE chat_id = ? AND state = 'active' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ))

    def get_paused_mandate(self, chat_id: str) -> Optional[dict]:
        """Pausierter Auftrag. Ersetzt den früheren Direktzugriff des Agenten auf
        die Verbindung, der am Lock vorbeilief."""
        return self._mandate_dict(self._read_one(
            "SELECT * FROM mandates WHERE chat_id = ? AND state = 'paused' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ))

    def get_any_active_mandate(self) -> Optional[dict]:
        """Gibt irgendeinen aktiven Auftrag zurück (für den Polling-Loop)."""
        return self._mandate_dict(self._read_one(
            "SELECT * FROM mandates WHERE state = 'active' ORDER BY id DESC LIMIT 1"
        ))

    def set_mandate_state(self, chat_id: str, state: str) -> bool:
        """Setzt den Zustand des aktiven Auftrags."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE mandates SET state = ?, updated_at = ? "
                "WHERE chat_id = ? AND state IN ('active','paused')",
                (state, _jetzt(), chat_id),
            )
            geaendert = cur.rowcount
        return geaendert > 0

    # --- Evaluations-Log ---

    def save_evaluation(self, listing_id: str, portal: str, mandate_id: int,
                        evaluation: dict) -> None:
        """Speichert das KI-Bewertungsergebnis inkl. vollständigem JSON.

        Ausschließlich für FACHLICHE Ergebnisse. Ein technischer Fehler ist keine
        Bewertung und darf hier nie landen — dafür ist eval_queue da.

        Angehängt statt ersetzt: match_log ist ein Verlaufsprotokoll. (Das frühere
        INSERT OR REPLACE war ohnehin wirkungslos, weil match_log nur über die
        laufende id eindeutig ist und nicht über listing_id/portal.)
        """
        with self._tx() as c:
            c.execute(
                """INSERT INTO match_log
                   (listing_id, portal, logged_at, bestanden, score,
                    ablehnungsgrund, geo_ok, evaluation, mandate_id)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    listing_id, portal, _jetzt(),
                    1 if evaluation.get("passt") else 0,
                    evaluation.get("score", 0),
                    evaluation.get("kurzfazit", ""),
                    json.dumps(evaluation, ensure_ascii=False, default=str),
                    mandate_id,
                ),
            )

    # --- Warteschlange für gescheiterte Bewertungen ---

    def enqueue_eval(self, listing_id: str, portal: str, mandate_id: Optional[int],
                     fehler: str, *, verbraucht_versuch: bool = True,
                     max_versuche: int = 3, retryable: bool = True,
                     next_retry_at: Optional[str] = None) -> None:
        """Vermerkt ein Inserat, dessen Bewertung technisch scheiterte.

        Je (listing_id, portal) existiert höchstens EIN Eintrag — der
        Primärschlüssel erzwingt das. Ein wiederholter Fehler aktualisiert also,
        statt eine zweite Zeile anzulegen.

        ``verbraucht_versuch=False`` (Auth-/Konfigurationsfehler) lässt den Zähler
        unberührt: Solche Fehler heilen nicht von selbst, sondern durch einen
        Eingriff. Würden sie zählen, wäre das Kontingent aufgebraucht, bevor
        jemand den Schlüssel erneuern konnte — die Inserate der Nacht wären
        endgültig verloren.
        """
        now = _jetzt()
        with self._tx() as c:
            row = c.execute(
                "SELECT retry_count FROM eval_queue WHERE listing_id = ? AND portal = ?",
                (listing_id, portal),
            ).fetchone()

            if row is None:
                versuche = 1 if verbraucht_versuch else 0
                status = self._eval_status(retryable, versuche, max_versuche)
                c.execute(
                    """INSERT INTO eval_queue
                       (listing_id, portal, mandate_id, status, retry_count,
                        next_retry_at, last_error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (listing_id, portal, mandate_id, status, versuche,
                     next_retry_at, fehler[:500], now, now),
                )
                return

            versuche = int(row["retry_count"]) + (1 if verbraucht_versuch else 0)
            status = self._eval_status(retryable, versuche, max_versuche)
            c.execute(
                """UPDATE eval_queue
                   SET mandate_id = COALESCE(?, mandate_id), status = ?, retry_count = ?,
                       next_retry_at = ?, last_error = ?, updated_at = ?
                   WHERE listing_id = ? AND portal = ?""",
                (mandate_id, status, versuche, next_retry_at, fehler[:500], now,
                 listing_id, portal),
            )

    @staticmethod
    def _eval_status(retryable: bool, versuche: int, max_versuche: int) -> str:
        """``retry_count`` zählt die bisherigen Fehlschläge, ``max_versuche`` die
        danach noch zulässigen WIEDERHOLUNGEN.

        Ein Protokollfehler (max_versuche = 1) bleibt nach dem ersten Fehlschlag
        also offen — genau ein weiterer Versuch — und gilt erst nach dem zweiten
        als endgültig gescheitert.
        """
        if not retryable:
            return EVAL_BLOCKED
        if versuche > max_versuche:
            return EVAL_FAILED
        return EVAL_PENDING

    def resolve_eval(self, listing_id: str, portal: str) -> None:
        """Nach erfolgreicher Bewertung: Eintrag entfällt. Die Warteschlange
        enthält damit ausschließlich offene Arbeit."""
        with self._tx() as c:
            c.execute(
                "DELETE FROM eval_queue WHERE listing_id = ? AND portal = ?",
                (listing_id, portal),
            )

    def reaktiviere_blockierte(self) -> int:
        """Blockierte Einträge wieder zur Bewertung freigeben.

        ``blocked`` bedeutet: Warten hilft nicht, es braucht einen Eingriff —
        typischerweise einen erneuerten API-Schlüssel. Ohne diesen Rückweg wäre
        ``blocked`` eine Sackgasse und die Inserate genau so verloren, wie es
        die Warteschlange verhindern soll.

        Der Zähler bleibt bei 0, weil ein Auth-Fehler nie einen Versuch
        verbraucht hat. Rückgabe: Anzahl der freigegebenen Einträge.

        Wer diese Freigabe auslöst, entscheidet der Aufrufer — in dieser Phase
        wird sie bewusst von keiner Automatik angestoßen.
        """
        with self._tx() as c:
            cur = c.execute(
                "UPDATE eval_queue SET status = ?, next_retry_at = NULL, updated_at = ? "
                "WHERE status = ?",
                (EVAL_PENDING, _jetzt(), EVAL_BLOCKED),
            )
            anzahl = cur.rowcount
        if anzahl:
            logger.info("%d blockierte Bewertungen wieder freigegeben", anzahl)
        return anzahl

    def get_eval_queue_entry(self, listing_id: str, portal: str) -> Optional[sqlite3.Row]:
        return self._read_one(
            "SELECT * FROM eval_queue WHERE listing_id = ? AND portal = ?",
            (listing_id, portal),
        )

    def get_due_evaluations(self, limit: int = 20, jetzt: Optional[str] = None) -> List[sqlite3.Row]:
        """Offene Bewertungen, deren Wartezeit abgelaufen ist — neueste zuerst.

        Bewusst LIFO: Wohnungsangebote sind verderbliche Ware. Bei einem Rückstau
        ist das jüngste Inserat dasjenige, bei dem eine Meldung überhaupt noch
        etwas nützt; das älteste ist ohnehin meist schon vergeben. Ein FIFO würde
        das knappe Retry-Budget zuerst für die aussichtslosen Fälle verbrauchen.

        Reine Leseabfrage — sie stößt von sich aus nichts an. Ob und wann erneut
        bewertet wird, entscheidet der Aufrufer.
        """
        return self._read(
            """
            SELECT q.*, l.url, l.titel, l.stadt, l.stadtteil, l.kaltmiete,
                   l.warmmiete, l.flaeche, l.zimmer, l.merkmale, l.seen_at,
                   l.notified_at
            FROM eval_queue q
            JOIN listings l ON l.id = q.listing_id AND l.portal = q.portal
            WHERE q.status = ?
              AND (q.next_retry_at IS NULL OR q.next_retry_at <= ?)
            ORDER BY q.created_at DESC
            LIMIT ?
            """,
            (EVAL_PENDING, jetzt or _jetzt(), limit),
        )

    def set_eval_status(self, listing_id: str, portal: str, status: str,
                        hinweis: Optional[str] = None) -> None:
        """Endzustand setzen, ohne den Versuchszähler anzufassen."""
        with self._tx() as c:
            c.execute(
                "UPDATE eval_queue SET status = ?, next_retry_at = NULL, "
                "last_error = COALESCE(?, last_error), updated_at = ? "
                "WHERE listing_id = ? AND portal = ?",
                (status, hinweis, _jetzt(), listing_id, portal),
            )

    def eval_queue_zaehler(self) -> dict:
        """Anzahl je Zustand — für die Gesundheitsanzeige und /zustand."""
        rows = self._read("SELECT status, COUNT(*) AS n FROM eval_queue GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    def eval_queue_groesse(self, status: Optional[str] = None) -> int:
        if status:
            row = self._read_one("SELECT COUNT(*) AS n FROM eval_queue WHERE status = ?", (status,))
        else:
            row = self._read_one("SELECT COUNT(*) AS n FROM eval_queue")
        return int(row["n"]) if row else 0

    # --- Chat-Verlauf (überlebt Neustarts) ---

    def append_chat(self, chat_id: str, role: str, content: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO chat_history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, _jetzt()),
            )

    def get_chat_history(self, chat_id: str, limit: int = 24) -> List[dict]:
        rows = self._read(
            "SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        # chronologisch zurückgeben (älteste zuerst)
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
