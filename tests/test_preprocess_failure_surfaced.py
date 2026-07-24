"""B02 H5 — a crashed preprocess build must NOT be presented to clients as a
finished session. Pins _run_preprocess's contract:

  - a build that RAISES  -> _preprocess_error carries the reason, an `error`
    topic is broadcast, and _preprocess_done is still set (waiting connects are
    unblocked rather than hung);
  - a build that SWALLOWS the failure inside run() (sets preprocessor.failed but
    doesn't raise) -> same: error surfaced, not silently served;
  - a SUCCESSFUL build -> no error, no error broadcast, duration pinned to the
    true DB end (MAX(offset_ms), payload-based — the source that replaced the old
    full-file scan's length, KdKK0D5G).

The fix landed in 30e3229; these are the regression guards that would catch a
reversion to "crashed build looks complete" (error stays None, no broadcast).
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.database import SessionDatabase
from app.processing.session import SessionEngine


def _engine(preprocessor, db=None) -> SessionEngine:
    e = SessionEngine.__new__(SessionEngine)   # bypass __init__ (asyncio tasks)
    e._session_name = "2026/1290_Test/11330_Qualifying"
    e._preprocessor = preprocessor
    e._preprocess_error = None
    e._db = db                                 # None when the pin is irrelevant
    e._duration = 0.0
    e._preprocess_done = asyncio.Event()
    e._baseline_ready = asyncio.Event()
    e._bg_tasks = set()
    e._broadcast = mock.AsyncMock()
    return e


def _fake_preprocessor(*, raises=None, failed=False):
    pp = mock.Mock()
    pp.run = mock.AsyncMock(side_effect=raises)
    pp.close = mock.Mock()
    pp.failed = failed
    return pp


def _broadcast_topics(engine) -> list[str]:
    return [c.args[0]["topic"] for c in engine._broadcast.call_args_list]


class PreprocessFailureSurfaced(unittest.IsolatedAsyncioTestCase):
    async def test_raised_build_error_is_surfaced_and_unblocks(self):
        e = _engine(_fake_preprocessor(raises=RuntimeError("boom")))
        await e._run_preprocess()
        self.assertEqual(e._preprocess_error, "boom")          # reason captured
        self.assertIn("error", _broadcast_topics(e))           # client told
        self.assertTrue(e._preprocess_done.is_set())           # connects unblocked

    async def test_swallowed_failure_is_still_surfaced(self):
        # run() returns normally but flagged the build as failed internally.
        e = _engine(_fake_preprocessor(raises=None, failed=True))
        await e._run_preprocess()
        self.assertEqual(e._preprocess_error, "pre-processing failed")
        self.assertIn("error", _broadcast_topics(e))
        self.assertTrue(e._preprocess_done.is_set())

    async def test_successful_build_reports_no_error(self):
        # A real DB with a message at 300 s so the finish pins _duration to the
        # true DB end (MAX(offset_ms)) — the source that replaced _scanned_duration.
        db = SessionDatabase(Path(tempfile.mkdtemp()))
        db.open()
        db.save_messages([(300_000, "10:05:00.000", "X", "{}")])
        try:
            e = _engine(_fake_preprocessor(raises=None, failed=False), db=db)
            await e._run_preprocess()
            self.assertIsNone(e._preprocess_error)                 # RIGHT: clean finish
            self.assertNotIn("error", _broadcast_topics(e))        # no false alarm
            self.assertTrue(e._preprocess_done.is_set())
            self.assertEqual(e._duration, 300.0)                   # pinned to true DB end
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
