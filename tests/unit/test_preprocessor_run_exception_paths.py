"""Coverage (critical path): the exception-handling branches of
`SessionPreProcessor.run()` — `asyncio.CancelledError` (logged, re-raised so
the caller's cancellation propagates correctly) and a generic `Exception`
(caught, surfaced via `self.failed`/`status=error`, AND re-raised per WB-4's
fix so a caller that doesn't explicitly check `.failed` can't mistake a
failed build for a successful one) — plus the end-of-session telemetry
`finalize_session` failure path, which is independently swallowed (logged
only) so one processor's failure at finalize doesn't crash an otherwise-
successful build.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _make_session(root: Path) -> Path:
    sess = root / "2026" / "1290_Test" / "11330_Qualifying"
    sess.mkdir(parents=True)
    si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
    (sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))
    (sess / "live.jsonl").write_text("")
    return sess


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


class RunExceptionPaths(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = _make_session(self.root)

    def _proc(self) -> SessionPreProcessor:
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            return SessionPreProcessor(self.sess, "Qualifying")

    async def test_cancelled_error_is_logged_and_reraised(self):
        p = self._proc()
        try:
            async def _boom(*a, **kw):
                raise asyncio.CancelledError()
                yield  # pragma: no cover - unreachable; keeps this an async generator

            with mock.patch("app.processing.preprocessor.read_jsonl", _boom):
                with self.assertLogs("app.processing.preprocessor", level="INFO") as log:
                    with self.assertRaises(asyncio.CancelledError):
                        await p.run()
                self.assertTrue(any("cancelled" in m.lower() for m in log.output))
        finally:
            p._db.close()

    async def test_generic_exception_is_caught_surfaced_and_reraised(self):
        # WB-4 (fix/wb4-preprocessor-run-failure-semantics) changed this
        # behavior: run() sets self.failed/status=error AND re-raises, so a
        # caller that doesn't explicitly check `.failed` (e.g.
        # LiveTimingFetcher.fetch_session) cannot mistake a build that failed
        # partway through for a successful one. This test originally asserted
        # the pre-WB-4 swallow-only behavior because it was written before
        # WB-4 was rebased into this branch; updated to match WB-4's
        # already-verified, already-red-gated fix (see
        # tests/test_wb4_preprocessor_run_failure_semantics.py).
        p = self._proc()
        try:
            async def _boom(*a, **kw):
                raise RuntimeError("kaboom")
                yield  # pragma: no cover

            with mock.patch("app.processing.preprocessor.read_jsonl", _boom):
                with self.assertRaises(RuntimeError):
                    await p.run()
            self.assertTrue(p.failed)
            self.assertEqual(p._db.get_meta("status"), "error")
        finally:
            p._db.close()

    async def test_telemetry_finalize_failure_is_logged_not_raised(self):
        """A failure in the end-of-session telemetry finalize must not crash
        an otherwise-successful build — only that step's output is lost."""
        lines = [_env("SessionInfo", "2026-07-18T10:00:00.000Z",
                       {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"})]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")
        p = self._proc()
        try:
            with mock.patch(
                "app.processing.processors.telemetry_processor.TelemetryProcessor.finalize_session",
                side_effect=RuntimeError("finalize boom"),
            ):
                with self.assertLogs("app.processing.preprocessor", level="ERROR") as log:
                    await p.run()
                self.assertTrue(any("finalize_session failed" in m for m in log.output))
            # The overall build still completed successfully despite the
            # finalize failure — it is logged, not surfaced as p.failed.
            self.assertFalse(p.failed)
            self.assertEqual(p._db.get_meta("status"), "complete")
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
