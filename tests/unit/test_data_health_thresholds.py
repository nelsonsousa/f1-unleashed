"""AC-5 (requirement-spec.md AC-5; file-impact-map.md §1 AC-5):
`POS_STALE_MS`/`CARDATA_STALE_MS` must become 9000ms (8000 + W, W=1.0s), NOT
5000ms (the SME's originally proposed and since-refuted value). No prior test
file exists for `data_health_processor.py` — this is a new module.

RED GATE: the current confirmed values are `POS_STALE_MS = 8000`,
`CARDATA_STALE_MS = 8000` (data_health_processor.py:42-43). Asserting the
target value (9000) fails against today's code — this is the whole point:
the fix is a two-constant change, and this test is the acceptance test for
it, run once now (observed to fail) and again after the fix lands.

Behavioral regression coverage (not requiring the buffer to exist — these
exercise DataHealthProcessor directly, driven the same way the D7-B red gate
test drives TelemetryProcessor, bypassing StreamNormalizer entirely):
  - a genuine outage (gap > either threshold) is still detected as stale
    under both today's 8000ms and the target 9000ms — the fix must not lose
    real-outage detection;
  - a normal-session-shaped gap that sits BETWEEN 8000ms and 9000ms is
    exactly the flicker case the spec cites 9000ms as curing — asserted
    directly as a before/after behavioral difference.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.data_health_processor import DataHealthProcessor

NUM = "44"


def _proc(session_type: str = "race") -> DataHealthProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    emitted: list[tuple[str, dict]] = []
    bus.emit = mock.Mock(side_effect=lambda topic, data, ts: emitted.append((topic, data)))
    proc = DataHealthProcessor(bus, session_type)
    proc.emitted_log = emitted
    return proc


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _car_data(num: str, speed: int = 200) -> dict:
    return {"Entries": [{"Cars": {num: {"Channels": {"2": speed, "4": 50, "5": 0}}}}]}


def _position(num: str, dp: float) -> dict:
    return {"Position": [{"Entries": {num: {"X": 0, "Y": 0}}}]}


GREEN_LEAD_S = 30.0   # comfortably past GREEN_GRACE_MS (15s) before `at`


def _seed_on_track_green(proc: DataHealthProcessor, num: str, at: datetime) -> None:
    """Bring the processor into a green, on-track, PAST-GRACE-PERIOD,
    assessing state for `num` as of `at`. Green transitions well before `at`
    (GREEN_LEAD_S, comfortably past GREEN_GRACE_MS=15s) so the post-green
    grace window doesn't mask the staleness comparison under test — a bug in
    an earlier version of this fixture that made the flicker-window test
    pass vacuously (grace suppressed evaluation, not the threshold)."""
    green_at = at - timedelta(seconds=GREEN_LEAD_S)
    proc._on_driver_list({num: {"tla": "VER"}}, green_at)
    proc._on_any(f"driverStatus:{num}", "TRACK", green_at)
    proc._on_track_status({"status": "green"}, green_at)
    proc._on_timing({"Lines": {num: {}}}, green_at)
    proc._on_position(_position(num, 0.0), at)
    proc._on_cardata(_car_data(num), at)


class ThresholdValues_RedGate(unittest.TestCase):
    def test_pos_and_cardata_stale_thresholds_are_9000ms_RED_GATE(self):
        self.assertEqual(
            DataHealthProcessor.POS_STALE_MS, 9000,
            "POS_STALE_MS must be 8000 + W (W=1.0s) = 9000ms, not the current 8000ms",
        )
        self.assertEqual(
            DataHealthProcessor.CARDATA_STALE_MS, 9000,
            "CARDATA_STALE_MS must be 8000 + W (W=1.0s) = 9000ms, not the current 8000ms",
        )

    def test_thresholds_are_not_the_refuted_5000ms_value(self):
        """5000ms is measured (artifact 022 §7) to catch 0 of 11 real outages
        that 8000ms already catches, and roughly doubles false-red flicker —
        explicitly refuted. This must hold regardless of the AC-5 fix state,
        so it is not itself expected to fail pre-fix."""
        self.assertNotEqual(DataHealthProcessor.POS_STALE_MS, 5000)
        self.assertNotEqual(DataHealthProcessor.CARDATA_STALE_MS, 5000)


class GenuineOutageDetectionUnaffected(unittest.TestCase):
    """A genuine outage (gap well past either 8000ms or 9000ms) must still be
    detected as stale regardless of which of the two threshold values is in
    effect — the fix must not weaken real-outage detection."""

    def test_position_gap_past_either_threshold_is_flagged_stale(self):
        proc = _proc()
        base = _t(0)
        _seed_on_track_green(proc, NUM, base)
        # Shanghai-P1-style extended gap: 71.5s, far past either 8000 or 9000ms.
        gap_end = base + timedelta(seconds=71.5)
        proc._on_cardata(_car_data(NUM), gap_end)   # keeps telemetry fresh
        proc._evaluate(gap_end)
        payload = proc.emitted_log[-1][1]
        self.assertNotEqual(payload["position"]["level"], "green")

    def test_cardata_gap_past_either_threshold_is_flagged_stale(self):
        proc = _proc()
        base = _t(0)
        _seed_on_track_green(proc, NUM, base)
        gap_end = base + timedelta(seconds=71.5)
        proc._on_position(_position(NUM, 0.0), gap_end)   # keeps position fresh
        proc._evaluate(gap_end)
        payload = proc.emitted_log[-1][1]
        self.assertNotEqual(payload["telemetry"]["level"], "green")


class FlickerWindowBehavioralDifference_RedGate(unittest.TestCase):
    """The specific 8000-9000ms window the AC-5 fix targets: a gap in this
    range is a false-red flicker under today's 8000ms threshold (an inflated
    measurement caused by a HELD `.z` entry, per the spec's own §2 AC-5
    mechanism description) but must read green once the threshold becomes
    9000ms. This test observes today's 8000ms value flag exactly such a gap
    as non-green — the behavior the fix must eliminate — and therefore is a
    RED GATE against the target (9000ms) behavior."""

    def test_8500ms_position_gap_is_flagged_stale_under_todays_threshold_RED_GATE(self):
        proc = _proc()
        base = _t(0)
        _seed_on_track_green(proc, NUM, base)
        gap_end = base + timedelta(milliseconds=8500)
        proc._on_cardata(_car_data(NUM), gap_end)
        proc._evaluate(gap_end)
        payload = proc.emitted_log[-1][1]
        # Correct/fixed (9000ms) expectation: an 8.5s gap must NOT be flagged.
        self.assertEqual(
            payload["position"]["level"], "green",
            "an 8.5s position gap is flagged non-green under today's 8000ms "
            "threshold — exactly the flicker AC-5's 9000ms fix must cure",
        )


if __name__ == "__main__":
    unittest.main()
