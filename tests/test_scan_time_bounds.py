"""KdKK0D5G — the SessionEngine anchors its playback clock from the DB's
persisted session start (processing_meta['start_time'], written by the
pre-processor from the first message) instead of a full pre-scan of live.jsonl.

Pins the new behaviour that replaced the old `_scan_time_bounds` full-file read:
- no clock until the start is recorded (we don't yet know the wall time);
- clock + start_time come from the DB meta;
- the anchor needs NO live.jsonl on disk (the scan is gone);
- `_ensure_clock` is idempotent and tolerant of a garbage meta value.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.processing.session import SessionEngine
from app.processing.database import SessionDatabase


def _engine(session_path: Path, db: SessionDatabase) -> SessionEngine:
    # Bypass __init__ (which sets up asyncio tasks) — _ensure_clock only touches
    # _clock, _db and _start_time.
    e = SessionEngine.__new__(SessionEngine)
    e._session_path = Path(session_path)
    e._db = db
    e._start_time = None
    e._clock = None
    e._duration = 0.0
    return e


class EnsureClockFromDb(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = SessionDatabase(self.dir)
        self.db.open()

    def tearDown(self):
        self.db.close()

    def test_no_start_meta_leaves_clock_unset(self):
        # Before the build records the start there is no clock — as specified,
        # "until the first message arrives we know nothing about the time".
        e = _engine(self.dir, self.db)
        e._ensure_clock()
        self.assertIsNone(e._clock)
        self.assertIsNone(e._start_time)

    def test_clock_anchored_from_db_meta_with_no_live_jsonl(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        self.db.set_meta("start_time", start.isoformat())
        # There is NO live.jsonl on disk — proves the anchor no longer scans it.
        self.assertFalse((self.dir / "live.jsonl").exists())
        e = _engine(self.dir, self.db)
        e._ensure_clock()
        self.assertIsNotNone(e._clock)
        self.assertEqual(e._start_time, start)

    def test_idempotent_and_never_re_anchors(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        self.db.set_meta("start_time", start.isoformat())
        e = _engine(self.dir, self.db)
        e._ensure_clock()
        first_clock = e._clock
        # A different meta value must not replace an already-anchored clock.
        self.db.set_meta("start_time", datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat())
        e._ensure_clock()
        self.assertIs(e._clock, first_clock)
        self.assertEqual(e._start_time, start)

    def test_garbage_meta_does_not_crash(self):
        self.db.set_meta("start_time", "not-a-timestamp")
        e = _engine(self.dir, self.db)
        e._ensure_clock()                      # RIGHT: no exception
        self.assertIsNone(e._clock)


if __name__ == "__main__":
    unittest.main()
