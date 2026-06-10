"""
Per-session SQLite database for processed timing data.

Stores pre-computed messages (topic + JSON payload) indexed by offset_ms.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Processed DBs are TRANSIENT scratch — built on demand, deleted when no client
# is viewing (kept in DEBUG). live.jsonl in the cache is the only permanent
# source. Each session gets a unique scratch file under ./tmp.
TRANSIENT_DB_DIR = Path("tmp")

# Topics excluded from the seek/connect state-restore snapshot. These are
# either per-lap on-demand fetches or high-rate / append-only histories that
# latest-per-topic restore can't represent (handled separately by the engine).
_RESTORE_EXCLUDE_PREFIXES = ("telemetryLap:", "liveTelemetry:")
_RESTORE_EXCLUDE_EXACT = frozenset({"position", "raceControlMessage"})


def transient_db_path(session_path: Path) -> Path:
    """Unique scratch DB path for a session: ./tmp/{year}_{event}_{session}.db."""
    name = "_".join(session_path.parts[-3:]) or session_path.name
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    return TRANSIENT_DB_DIR / f"{safe}.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    offset_ms    INTEGER NOT NULL,
    wall_clock   TEXT,
    topic        TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_topic_offset ON messages (topic, offset_ms);

CREATE TABLE IF NOT EXISTS telemetry (
    driver           TEXT NOT NULL,
    lap              INTEGER NOT NULL,
    offset_ms        INTEGER NOT NULL,
    start_wall_clock TEXT,
    end_wall_clock   TEXT,
    data             TEXT NOT NULL,
    PRIMARY KEY (driver, lap)
);

CREATE TABLE IF NOT EXISTS processing_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""

# Expected column order per table. If an existing DB has a different
# column layout (e.g. an older version of the schema that we extended
# via ALTER TABLE), we DROP+CREATE on open so the reprocess writes
# columns in the desired order. The next reprocess always fully
# repopulates the DB so data loss is fine.
EXPECTED_COLUMNS = {
    "messages": ["offset_ms", "wall_clock", "topic", "data"],
    "telemetry": [
        "driver", "lap", "offset_ms",
        "start_wall_clock", "end_wall_clock", "data",
    ],
}


class SessionDatabase:
    """SQLite database for a single session."""

    def __init__(self, session_path: Path, db_path: Optional[Path] = None):
        # Default to the transient scratch path; callers may override (e.g.
        # debug/inspection to a fixed location).
        self._db_path = db_path or transient_db_path(session_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        """Open database connection and create schema. If existing
        tables have a column order that doesn't match EXPECTED_COLUMNS
        (e.g. older DB extended via prior ALTER TABLEs), drop and
        recreate them so the next reprocess writes columns in the
        desired order."""
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for table, expected in EXPECTED_COLUMNS.items():
            rows = self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            if rows:
                cur = [r[1] for r in rows]
                if cur != expected:
                    self._conn.execute(f"DROP TABLE {table}")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def reset(self) -> None:
        """Clear all processed data so a fresh build can be written.

        A build always re-reads the whole live.jsonl, so the DB is
        truncated rather than appended to — this prevents duplicate
        rows when a build re-runs (e.g. a live capture restarts
        mid-session, or an interrupted build is retried).
        """
        self._conn.execute("DELETE FROM messages")
        self._conn.execute("DELETE FROM telemetry")
        self._conn.commit()

    # ── Messages ──

    def save_messages(self, messages: list[tuple]) -> None:
        """Batch insert messages. Each tuple is either
        (offset_ms, topic, data_json) — for backward compatibility —
        or (offset_ms, wall_clock, topic, data_json) in column order."""
        if messages and len(messages[0]) == 4:
            self._conn.executemany(
                "INSERT INTO messages (offset_ms, wall_clock, topic, data) "
                "VALUES (?, ?, ?, ?)",
                messages,
            )
        else:
            self._conn.executemany(
                "INSERT INTO messages (offset_ms, topic, data) VALUES (?, ?, ?)",
                messages,
            )
        self._conn.commit()

    def get_state_at(self, offset_ms: int) -> dict[str, Any]:
        """Get latest message per topic at or before offset_ms.

        Returns {topic: {"data": ..., "offset_ms": ...}}.
        """
        rows = self._conn.execute(
            """SELECT topic, data, offset_ms FROM messages
               WHERE rowid IN (
                   SELECT MAX(rowid) FROM messages
                   WHERE offset_ms <= ?
                   GROUP BY topic
               )""",
            (offset_ms,),
        ).fetchall()
        out = {}
        for topic, data, ofs in rows:
            if topic in _RESTORE_EXCLUDE_EXACT:
                continue
            if any(topic.startswith(p) for p in _RESTORE_EXCLUDE_PREFIXES):
                continue
            out[topic] = {"data": json.loads(data), "offset_ms": ofs}
        return out

    def get_messages_in_range(
        self, from_ms: int, to_ms: int
    ) -> list[tuple[int, str, Any]]:
        """Get messages between from_ms (exclusive) and to_ms (inclusive)."""
        rows = self._conn.execute(
            """SELECT offset_ms, topic, data FROM messages
               WHERE offset_ms > ? AND offset_ms <= ?
               ORDER BY offset_ms""",
            (from_ms, to_ms),
        ).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    # ── Telemetry ──

    def save_telemetry(
        self,
        driver: str,
        lap: int,
        offset_ms: int,
        data_json: str,
        start_wall_clock: Optional[str] = None,
        end_wall_clock: Optional[str] = None,
    ) -> None:
        """Upsert a completed lap's telemetry trace. offset_ms is the
        emit-time offset (= lap end). start_wall_clock / end_wall_clock
        are HH:MM:SS.SSS strings bounding the lap's sample window."""
        self._conn.execute(
            "INSERT OR REPLACE INTO telemetry "
            "(driver, lap, offset_ms, start_wall_clock, end_wall_clock, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (driver, lap, offset_ms, start_wall_clock, end_wall_clock, data_json),
        )
        self._conn.commit()

    def get_telemetry(self, driver: str, lap: int) -> Optional[Any]:
        """Fetch a completed lap's telemetry samples, or None."""
        row = self._conn.execute(
            "SELECT data FROM telemetry WHERE driver = ? AND lap = ?",
            (driver, lap),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_all_topics(self) -> list[str]:
        """Get list of all distinct topics."""
        rows = self._conn.execute(
            "SELECT DISTINCT topic FROM messages"
        ).fetchall()
        return [r[0] for r in rows]

    # ── Processing Metadata ──

    def set_meta(self, key: str, value: str) -> None:
        """Set a processing metadata value."""
        self._conn.execute(
            "INSERT OR REPLACE INTO processing_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        """Get a processing metadata value."""
        row = self._conn.execute(
            "SELECT value FROM processing_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None


def is_db_complete(session_path: Path) -> bool:
    """True if the session's processed DB exists and finished building.

    Used to decide whether a session counts as fully downloaded — a
    session.db whose status is not 'complete' is still being built (or
    its build was interrupted).
    """
    db_path = session_path / "session.db"
    if not db_path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM processing_meta WHERE key = 'status'"
        ).fetchone()
        return row is not None and row[0] == "complete"
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()
