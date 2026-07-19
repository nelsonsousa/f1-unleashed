"""B02 H5 — a crashed preprocess build must NOT be presented to clients as a
finished session. Pins _run_preprocess's contract:

  - a build that RAISES  -> _preprocess_error carries the reason, an `error`
    topic is broadcast, and _preprocess_done is still set (waiting connects are
    unblocked rather than hung);
  - a build that SWALLOWS the failure inside run() (sets preprocessor.failed but
    doesn't raise) -> same: error surfaced, not silently served;
  - a SUCCESSFUL build -> no error, no error broadcast, duration pinned to the
    full scanned length.

The fix landed in 30e3229; these are the regression guards that would catch a
reversion to "crashed build looks complete" (error stays None, no broadcast).
"""
import asyncio
import unittest
from unittest import mock

from app.processing.session import SessionEngine


def _engine(preprocessor) -> SessionEngine:
    e = SessionEngine.__new__(SessionEngine)   # bypass __init__ (asyncio tasks)
    e._session_name = "2026/1290_Test/11330_Qualifying"
    e._preprocessor = preprocessor
    e._preprocess_error = None
    e._scanned_duration = 123.0
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
        e = _engine(_fake_preprocessor(raises=None, failed=False))
        await e._run_preprocess()
        self.assertIsNone(e._preprocess_error)                 # RIGHT: clean finish
        self.assertNotIn("error", _broadcast_topics(e))        # no false alarm
        self.assertTrue(e._preprocess_done.is_set())
        self.assertEqual(e._duration, 123.0)                   # pinned to scanned length


if __name__ == "__main__":
    unittest.main()
