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
# source. Each session gets a unique scratch file under TMP_DIR (card 25 —
# OS-appropriate location, configurable via F1U_DATA_DIR).
from app.config import TMP_DIR as TRANSIENT_DB_DIR

# Topics excluded from the seek/connect state-restore snapshot. These are
# either per-lap on-demand fetches or high-rate / append-only histories that
# latest-per-topic restore can't represent (handled separately by the engine).
_RESTORE_EXCLUDE_PREFIXES = ("telemetryLap:", "liveTelemetry:")
_RESTORE_EXCLUDE_EXACT = frozenset({"position", "raceControlMessage"})


def transient_db_path(session_path: Path) -> Path:
    """Unique scratch DB path for a session under TMP_DIR (= DATA_DIR/tmp):
    {year}_{event}_{session}.db."""
    name = "_".join(session_path.parts[-3:]) or session_path.name
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    TRANSIENT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return TRANSIENT_DB_DIR / f"{safe}.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    offset_ms    INTEGER NOT NULL,
    wall_clock   TEXT,
    topic        TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_topic_offset ON messages (topic, offset_ms);
-- offset_ms alone: the playback loop's range query runs 60x/s and _data_edge_ms'
-- MAX(offset_ms) every second; without this they can't use the (topic,offset_ms)
-- index (topic leads) and full-scan the table. (B04 fiodUrGN)
CREATE INDEX IF NOT EXISTS idx_msg_offset ON messages (offset_ms);

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
        self._conn.commit()

    # ── Messages ──

    def save_messages(self, messages: list[tuple]) -> None:
        """Batch insert messages; each tuple is
        (offset_ms, wall_clock, topic, data_json) in column order."""
        self._conn.executemany(
            "INSERT INTO messages (offset_ms, wall_clock, topic, data) "
            "VALUES (?, ?, ?, ?)",
            messages,
        )
        self._conn.commit()

    def get_state_at(self, offset_ms: int) -> dict[str, Any]:
        """Get latest message per topic at or before offset_ms.

        Returns {topic: {"data": ..., "offset_ms": ...}}.
        """
        # Per-topic index seeks instead of a full GROUP-BY scan of the whole
        # table. The old MAX(rowid) GROUP BY plans as a full covering scan of
        # idx_msg_topic_offset (offset-independent, ~100ms on 500k rows) and then
        # discards the dominant excluded topics (position, telemetryLap:) in
        # Python. Here the excludes are skipped BEFORE querying, and each topic's
        # latest row <= offset is a fast index seek. (B04 JiRvaHNt)
        out = {}
        for (topic,) in self._conn.execute("SELECT DISTINCT topic FROM messages"):
            if topic in _RESTORE_EXCLUDE_EXACT:
                continue
            if any(topic.startswith(p) for p in _RESTORE_EXCLUDE_PREFIXES):
                continue
            row = self._conn.execute(
                "SELECT data, offset_ms FROM messages "
                "WHERE topic = ? AND offset_ms <= ? ORDER BY offset_ms DESC LIMIT 1",
                (topic, offset_ms),
            ).fetchone()
            if row is not None:
                out[topic] = {"data": json.loads(row[0]), "offset_ms": row[1]}
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
    # Completed-lap telemetry lives in the `messages` table as
    # `telemetryLap:{driver}:{lap}` rows; there is no separate telemetry table.

    def get_telemetry(self, driver: str, lap: int) -> Optional[Any]:
        """Fetch a completed lap's telemetry samples from the messages table
        (topic telemetryLap:{driver}:{lap}), or None."""
        row = self._conn.execute(
            "SELECT data FROM messages WHERE topic = ? ORDER BY offset_ms DESC LIMIT 1",
            (f"telemetryLap:{driver}:{lap}",),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_topic_history(self, topic: str, max_offset_ms: int) -> list[Any]:
        """All payloads for a topic at or before offset, chronological — for
        append-only histories (e.g. raceControlMessage) that the latest-per-
        topic restore can't represent."""
        rows = self._conn.execute(
            "SELECT data FROM messages WHERE topic = ? AND offset_ms <= ? ORDER BY offset_ms",
            (topic, max_offset_ms),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_topic_prefix_history(self, prefix: str,
                                 max_offset_ms: int) -> list[tuple[str, Any]]:
        """(topic, payload) for every message whose topic starts with `prefix`,
        at or before offset, chronological — for per-driver append-style
        histories (e.g. driverLaps:*) that the client accumulates and that the
        latest-per-topic restore can't represent."""
        # Range predicate instead of LIKE 'prefix%': LIKE can't use the
        # (topic, offset_ms) index and full-scans the table on every seek; a
        # half-open range [prefix, prefix+'\uffff') seeks the index. '\uffff' is
        # greater than any byte in our ASCII topics, so it bounds exactly the
        # prefix-matching rows. (B04 wqMnjDSk)
        rows = self._conn.execute(
            "SELECT topic, data FROM messages "
            "WHERE topic >= ? AND topic < ? AND offset_ms <= ? "
            "ORDER BY offset_ms",
            (prefix, prefix + "\uffff", max_offset_ms),
        ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def list_lap_telemetry(self, max_offset_ms: int) -> list[tuple[str, int, int]]:
        """(driver, lap, data_length) for every telemetryLap row at or before
        offset — feeds the client's telemetry-availability map."""
        # Range predicate (index seek) instead of LIKE (full scan). (B04 wqMnjDSk)
        rows = self._conn.execute(
            "SELECT topic, length(data) FROM messages "
            "WHERE topic >= ? AND topic < ? AND offset_ms <= ?",
            ("telemetryLap:", "telemetryLap:\uffff", max_offset_ms),
        ).fetchall()
        out = []
        for topic, dlen in rows:
            parts = topic.split(":")
            if len(parts) == 3:
                try:
                    out.append((parts[1], int(parts[2]), dlen or 0))
                except ValueError:
                    pass
        return out

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
