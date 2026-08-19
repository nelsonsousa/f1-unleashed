"""Regression test for Trello card 5Ud9mKuk ("Pit-entry dp discontinuity
(dp≈97.3-97.5, Spa) causes monotonic-dp filter to reject valid positions").

Real-data evidence (docs/artifacts/2026-07-29-026-post-candidate-a-telemetry-gaps/
data-investigation.md §5b, project root): `telemetryLap:41:18` (2,680 ms) and
`telemetryLap:44:6` (2,299 ms) are contiguous null-dp runs of exactly 7 and 5
samples. In both, the raw feed is healthy (a fresh preceding Position.z entry
20-260 ms before every CarData.z entry in the window) — the cause is
downstream, in `position_processor.project_local`: at dp ~= 97.3-97.5 (the
Spa pit-entry / final-chicane region, where the physical pit lane runs
parallel and close to the track over an extended stretch), a car diverging
onto the pit road — before the timing feed's own PIT status latches `in_pit`,
which is what already tolerates spurious pit-lane projections everywhere else
— nearest-point-projects onto a track-polyline point slightly BEHIND its true
position: car 41 goes 97.526 -> 97.295 (delta 0.231), car 44 goes
97.455 -> 97.303 (delta 0.152). `telemetry_processor._handle_position`'s
monotonic `elif dp > prev:` filter then refuses every subsequent position
(prev is frozen at the old, spurious maximum) until dp climbs back past it —
exactly 7 and exactly 5 consecutive skips, matching the observed gap
durations exactly.

THIS IS THE RED GATE (rules/testing.md): written directly from the
investigation artifact's numbers, against the unfixed `_handle_position`,
asserting the CORRECT/fixed behavior (a small, bounded, single-hop backward
drift late in the lap is accepted, not treated as invalid data) — expected to
FAIL against today's unconditional `elif dp > prev:` filter. No proposed fix
was read before writing this test.

Scope note: the fix must NOT loosen the monotonic filter generally — a
backward jump mid-lap, or one exceeding the small late-lap tolerance, remains
rejected exactly as before (non-regression tests below cover both).
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.telemetry_processor import TelemetryProcessor

CAR_41 = "41"
CAR_44 = "44"


def _proc() -> TelemetryProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return TelemetryProcessor(bus, "qualifying")


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 14, 5, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _feed(proc: TelemetryProcessor, num: str, dp: float, ts: datetime) -> None:
    proc._handle_position({num: [0.0, 0.0, dp]}, ts)


class PitEntryDpDiscontinuityAcceptedNotRejected(unittest.TestCase):
    """Reproduces telemetryLap:41:18 exactly: a 97.526 -> 97.295 pit-entry
    projection discontinuity, followed by 6 more samples climbing back up
    that stayed <= the frozen 97.526 max under the unfixed code (7 skips
    total), then a normal recovery sample above it."""

    def test_car41_lap41_18_sequence_all_accepted_not_skipped(self):
        proc = _proc()
        drv = proc._drv(CAR_41)
        drv.activated = True
        drv.last_dp = 97.526
        drv.last_pos_ts = _t(0.0)

        # Exact shape from the investigation: the discontinuity sample itself,
        # then 6 more samples climbing back toward (but under, under the OLD
        # code) the frozen max, then one clearly past it.
        raw_dps = [97.295, 97.32, 97.35, 97.38, 97.41, 97.44, 97.50, 97.55]

        for i, dp in enumerate(raw_dps, start=1):
            _feed(proc, CAR_41, dp, _t(float(i) * 0.268))

        pushed = [e[1] for e in drv.pos_buffer]
        self.assertEqual(
            pushed, raw_dps,
            "every sample in the pit-entry discontinuity sequence must be "
            "accepted (pushed) -- none may be skipped as an invalid backward "
            "jump, since this is a bounded, recoverable projection artifact "
            "at a known late-lap dp region, not invalid data",
        )
        self.assertAlmostEqual(drv.last_dp, raw_dps[-1])


class PitEntryDpDiscontinuityCar44(unittest.TestCase):
    """Reproduces telemetryLap:44:6: 97.455 -> 97.303 (delta 0.152), 5 skips
    under the unfixed code."""

    def test_car44_lap44_6_sequence_all_accepted_not_skipped(self):
        proc = _proc()
        drv = proc._drv(CAR_44)
        drv.activated = True
        drv.last_dp = 97.455
        drv.last_pos_ts = _t(0.0)

        raw_dps = [97.303, 97.35, 97.40, 97.42, 97.45, 97.50, 97.60]

        for i, dp in enumerate(raw_dps, start=1):
            _feed(proc, CAR_44, dp, _t(float(i) * 0.24))

        pushed = [e[1] for e in drv.pos_buffer]
        self.assertEqual(pushed, raw_dps)
        self.assertAlmostEqual(drv.last_dp, raw_dps[-1])


class MidLapBackwardJumpStillRejected(unittest.TestCase):
    """Non-regression: a backward dp jump NOT late in the lap (prev well
    below WRAP_HIGH) must remain rejected exactly as before -- the fix is
    scoped to the known late-lap pit-entry region, not a blanket loosening of
    the monotonic filter."""

    def test_mid_lap_small_backward_jump_is_still_skipped(self):
        proc = _proc()
        drv = proc._drv(CAR_41)
        drv.activated = True
        drv.last_dp = 50.0
        drv.last_pos_ts = _t(0.0)

        _feed(proc, CAR_41, 49.9, _t(0.25))   # small backward jump, mid-lap

        self.assertEqual(len(drv.pos_buffer), 0, "mid-lap backward jumps are not this defect")
        self.assertEqual(drv.last_dp, 50.0)


class LateLapLargeBackwardJumpStillRejected(unittest.TestCase):
    """Non-regression: even late in the lap (prev > WRAP_HIGH), a backward
    jump LARGER than the small bounded tolerance must remain rejected -- the
    fix must not open the door to arbitrary invalid backward data at any
    magnitude, only the small, evidence-bounded discontinuity actually
    observed (<= 0.231 in the real data)."""

    def test_late_lap_large_backward_jump_is_still_skipped(self):
        proc = _proc()
        drv = proc._drv(CAR_41)
        drv.activated = True
        drv.last_dp = 97.526
        drv.last_pos_ts = _t(0.0)

        _feed(proc, CAR_41, 80.0, _t(0.25))   # large, implausible backward jump

        self.assertEqual(len(drv.pos_buffer), 0)
        self.assertEqual(drv.last_dp, 97.526)


class RealSfWrapStillWorks(unittest.TestCase):
    """Non-regression: the genuine S/F wrap path (prev > WRAP_HIGH, dp <
    WRAP_LOW) is untouched by the new late-lap tolerance branch."""

    def test_wrap_from_high_dp_to_low_dp_still_closes_a_crossing(self):
        proc = _proc()
        drv = proc._drv(CAR_41)
        drv.activated = True
        drv.last_dp = 99.5
        drv.last_pos_ts = _t(0.0)

        _feed(proc, CAR_41, 0.5, _t(0.25))

        self.assertEqual(len(drv.crossings), 1)
        self.assertEqual(len(drv.pos_buffer), 1)
        self.assertEqual(drv.pos_buffer[0][1], 0.5)
        self.assertEqual(drv.last_dp, 0.5)


if __name__ == "__main__":
    unittest.main()
