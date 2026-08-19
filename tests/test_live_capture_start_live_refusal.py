"""Trello c966lztz: LiveCaptureService.start_live's single-capture refusal (M5)
must be distinguishable from success at the source, not just handled correctly
by the one caller that happens to check today.

Covers the refusal branch directly (app/services/live_capture.py's
`return sid, False`), which the monitor-level regression test in
tests/test_main_scheduler_settings.py::StartLiveRefusalBookkeeping exercises
only through a mock and therefore never actually runs.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from app.services.live_capture import LiveCaptureService


class StartLiveRefusal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.service = LiveCaptureService(cache_dir=self._tmpdir.name)

    async def test_refusal_returns_existing_sid_with_started_false(self):
        # Simulate an already-active capture without going through the real
        # SignalR-backed capture loop: a not-yet-done task registered under
        # an existing session id is exactly what the refusal check looks at.
        active_sid = "already-active-sid"
        never_finishes = asyncio.get_event_loop().create_future()
        self.addCleanup(never_finishes.cancel)
        task = asyncio.ensure_future(self._wrap(never_finishes))
        self.addCleanup(task.cancel)
        self.service._tasks[active_sid] = task

        result = await self.service.start_live(
            year=2026, meeting_name="Test GP", session_type="Race",
        )

        self.assertEqual(
            result, (active_sid, False),
            "refusal must return (existing_sid, started=False), distinguishable "
            "from a real start -- a caller that unpacks (sid, started) and only "
            "adopts new bookkeeping when started is True must never mistake this "
            "for success")
        # No new capture was registered: only the pre-existing active one.
        self.assertEqual(list(self.service._tasks.keys()), [active_sid])
        self.assertEqual(self.service._captures, {})

    async def _wrap(self, future):
        await future

    async def test_start_succeeds_when_no_capture_is_active(self):
        session_id, started = await self.service.start_live(
            year=2026, meeting_name="Test GP", session_type="Race",
            round_number=1, meeting_key=42, session_name="Race", session_key=99,
        )
        self.addCleanup(self.service._tasks[session_id].cancel)

        self.assertTrue(started)
        self.assertIn(session_id, self.service._captures)
        self.assertIn(session_id, self.service._tasks)


if __name__ == "__main__":
    unittest.main()
