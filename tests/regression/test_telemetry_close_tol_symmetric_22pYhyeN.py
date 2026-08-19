"""Regression test for Trello card 22pYhyeN ("Lap-boundary detection: _CLOSE_TOL
is one-sided, rejects future crossings — real lap loss today").

Real-data evidence (docs/artifacts/2026-07-28-019-telemetry-delay-blast-radius/
data-investigation.md §6.2, project root): on the current build, car 41's
Safety-Car lap was closed by `_try_close` against a buffered S/F crossing
**76.26 seconds AFTER** the `driverLaps.currentLap` report that triggered the
close (`gap = report_ts - crossings[-1] = -76.26 s`, the single negative-gap
close in the whole race). `_try_close`'s deferral test,

    drv.crossings[-1] < report_ts - self._CLOSE_TOL

only rejects a crossing that is too OLD relative to `report_ts` (defers until
a newer one arrives); it places no upper bound on how far in the FUTURE a
crossing may be and still be accepted as "the" closing crossing for lap `m`.
A crossing that far ahead of the report is not lap m's own closing crossing —
it is very likely a LATER lap's crossing (a real one is genuinely missing
from the source data in this window, per the investigation) — so accepting
it: (a) gives lap m a bogus, inflated duration bounded by the wrong crossing,
and (b) discards the true earlier crossing from `drv.crossings` (`_try_close`
prunes to `[crossings[-1]]` on every successful close), leaving nothing to
bound the NEXT lap. On the real data this is exactly what happened: ARM1
closed the fused lap as "lap 23" against the future crossing and then lost
lap 24 entirely (deferred forever, never closed) — every other car on track
labelled the same Safety-Car lap "24".

THIS IS THE RED GATE (rules/testing.md): written against the unfixed
`_try_close`, asserting the CORRECT/fixed behavior (a crossing more than
`_CLOSE_TOL` ahead of `report_ts` must be treated exactly like one too far in
the past — deferred, not accepted) which is expected to FAIL against today's
one-sided guard. No proposed fix was read before writing this test.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.telemetry_processor import TelemetryProcessor

NUM = "41"


def _proc() -> TelemetryProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return TelemetryProcessor(bus, "race")


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


class TryCloseRejectsCrossingTooFarInFuture(unittest.TestCase):
    """A crossing arriving `_CLOSE_TOL` (60s) or more AFTER `report_ts` must be
    deferred, exactly as a crossing that far BEFORE `report_ts` already is —
    it is not plausibly lap m's own closing crossing."""

    def test_crossing_76s_ahead_of_report_is_deferred_not_closed_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True

        # Real-data shape: an earlier crossing (lap m's START boundary) plus a
        # second crossing that lands 76.26s AFTER the timing report claiming
        # lap m is complete — matching the investigation's measured gap.
        report_ts = _t(1000.0)
        future_crossing = report_ts + timedelta(seconds=76.26)
        drv.crossings = [_t(890.0), future_crossing]
        drv.committed = 0

        proc._try_close(drv, 1, report_ts)

        # Correct/fixed behavior: defer — do not treat `future_crossing` as
        # lap 1's closing boundary. Nothing should be emitted, `committed`
        # must stay at 0, and the pending state must record the deferral so
        # `_handle_position` retries once a genuine, in-window crossing
        # arrives (mirrors the existing "too old" deferral path exactly).
        self.assertEqual(
            proc._bus.emit.call_args_list, [],
            "a crossing 76.26s in the future of report_ts must not be "
            "accepted as lap m's closing crossing (it wrongly closed the "
            "lap and discarded the true earlier boundary on real data)",
        )
        self.assertEqual(drv.committed, 0)
        self.assertEqual(drv.pending_lap, 1)
        self.assertEqual(drv.pending_report_ts, report_ts)
        # The crossing buffer must be left untouched while deferred (the
        # earlier crossing must NOT be silently dropped).
        self.assertEqual(drv.crossings, [_t(890.0), future_crossing])

    def test_crossing_exactly_at_the_future_tolerance_boundary_still_closes(self):
        """The window is inclusive at the edge (mirrors the existing
        `< report_ts - _CLOSE_TOL` past-side check, which excludes exactly
        `report_ts - _CLOSE_TOL` from deferral) -- a crossing exactly
        `_CLOSE_TOL` ahead of report_ts is still close enough to be genuine
        and must close normally, not defer."""
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True

        report_ts = _t(1000.0)
        boundary_crossing = report_ts + proc._CLOSE_TOL
        drv.crossings = [_t(890.0), boundary_crossing]
        drv.committed = 0

        proc._try_close(drv, 1, report_ts)

        self.assertEqual(len(proc._bus.emit.call_args_list), 1)
        topic, _out, _ts = proc._bus.emit.call_args_list[0].args
        self.assertEqual(topic, f"telemetryLap:{NUM}:1")
        self.assertEqual(drv.committed, 1)
        self.assertIsNone(drv.pending_lap)

    def test_crossing_just_past_the_future_tolerance_defers(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True

        report_ts = _t(1000.0)
        just_too_future = report_ts + proc._CLOSE_TOL + timedelta(milliseconds=1)
        drv.crossings = [_t(890.0), just_too_future]
        drv.committed = 0

        proc._try_close(drv, 1, report_ts)

        self.assertEqual(proc._bus.emit.call_args_list, [])
        self.assertEqual(drv.committed, 0)
        self.assertEqual(drv.pending_lap, 1)

    def test_existing_too_old_deferral_is_unaffected(self):
        """Non-regression for the pre-existing (correct) too-OLD guard: a
        crossing more than `_CLOSE_TOL` BEFORE report_ts must still defer,
        exactly as before this fix."""
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True

        report_ts = _t(1000.0)
        too_old_crossing = report_ts - proc._CLOSE_TOL - timedelta(seconds=1)
        drv.crossings = [_t(800.0), too_old_crossing]
        drv.committed = 0

        proc._try_close(drv, 1, report_ts)

        self.assertEqual(proc._bus.emit.call_args_list, [])
        self.assertEqual(drv.committed, 0)
        self.assertEqual(drv.pending_lap, 1)

    def test_normal_in_window_crossing_still_closes(self):
        """Non-regression for the ordinary case: a crossing shortly BEFORE
        report_ts (the typical "report lags crossing" shape) still closes
        normally."""
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True

        report_ts = _t(1000.0)
        normal_crossing = report_ts - timedelta(seconds=0.3)
        drv.crossings = [_t(890.0), normal_crossing]
        drv.committed = 0

        proc._try_close(drv, 1, report_ts)

        self.assertEqual(len(proc._bus.emit.call_args_list), 1)
        self.assertEqual(drv.committed, 1)
        self.assertIsNone(drv.pending_lap)


if __name__ == "__main__":
    unittest.main()
