"""D7-B pairing buffer (`TelemetryProcessor._push_pos`/`_nearest_unconsumed_pos`,
renamed from `_nearest_preceding_pos` in fix-attempt 3 when single-use
consumption was added): unit coverage for the branches the main red-gate
regression (tests/regression/test_telemetry_pairing_yield_d7b.py) doesn't
reach — pre-activation buffering, in-pit buffering, a CarData entry with no
eligible (preceding, unconsumed) position, and the bounded buffer's eviction.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.telemetry_processor import TelemetryProcessor

NUM = "1"


def _proc(session_type="practice") -> TelemetryProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return TelemetryProcessor(bus, session_type)


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


class PreActivationAndInPitBuffering(unittest.TestCase):
    def test_position_before_activation_is_buffered_not_stored_as_sample(self):
        proc = _proc()
        drv = proc._drv(NUM)
        self.assertFalse(drv.activated)
        proc._handle_position({NUM: [0, 0, 10.0]}, _t(0))
        self.assertEqual(len(drv.pos_buffer), 1)
        # CarData pairs (live passthrough) even pre-activation, but no sample stored.
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 200}}}}]}, _t(0.1))
        self.assertEqual(drv.samples, [])

    def test_position_in_pit_is_buffered_even_when_dp_stale(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        drv.in_pit = True
        drv.last_dp = 5.0
        proc._handle_position({NUM: [0, 0, 5.0]}, _t(0))   # stale dp, but in_pit
        self.assertEqual(len(drv.pos_buffer), 1)


class NoEligiblePosition(unittest.TestCase):
    """AC-3 (requirement-spec.md, this repo's later Candidate A work): a
    CarData entry with no eligible (preceding, unconsumed) position no
    longer results in nothing stored — it persists as a dp=None row
    (channels still captured), per the emit-unpaired rule. These two tests
    predate AC-3 and originally asserted the OLD skip behavior; updated here
    to match — see `EmitUnpairedInsteadOfSkip_AC3` below, which covers the
    same "no eligible position" precondition as
    `test_car_data_before_any_position_is_skipped` and is this class's
    actual acceptance test for the new behavior."""

    def test_car_data_before_any_position_is_skipped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 200}}}}]}, _t(0))
        self.assertEqual(len(drv.samples), 1)
        self.assertIsNone(drv.samples[0][0])

    def test_car_data_earlier_than_all_buffered_positions_is_skipped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        proc._handle_position({NUM: [0, 0, 10.0]}, _t(5))   # position at t=5s
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 200}}}}]}, _t(1))  # CarData at t=1s
        self.assertEqual(len(drv.samples), 1)
        self.assertIsNone(drv.samples[0][0])


class BoundedBufferEviction(unittest.TestCase):
    def test_buffer_never_exceeds_max_and_keeps_the_newest(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        n = TelemetryProcessor._POS_BUFFER_MAX + 20
        dp = 0.0
        for i in range(n):
            dp = (dp + 0.1) % 80.0   # stay well under WRAP_HIGH to avoid lap-boundary logic
            proc._handle_position({NUM: [0, 0, dp]}, _t(i * 0.1))
        self.assertLessEqual(len(drv.pos_buffer), TelemetryProcessor._POS_BUFFER_MAX)
        # The newest pushed position must still be present (eviction drops the OLDEST).
        newest_ts_ms = drv.pos_buffer[-1][0]
        self.assertEqual(newest_ts_ms, drv.pos_buffer[-1][0])
        self.assertTrue(drv.pos_buffer[0][0] < newest_ts_ms)


class EmitUnpairedInsteadOfSkip_AC3(unittest.TestCase):
    """AC-3 (requirement-spec.md AC-3): a CarData entry with no eligible
    unconsumed position must PERSIST with `dp=None` (channels still valid),
    not be silently skipped as today's code does
    (telemetry_processor.py:539-541, `if match is None: continue`).

    Feature test derived from the acceptance criterion, not from a proposed
    fix (no fix exists yet) — currently FAILS because `drv.samples` stays
    empty; the fix must instead append a `[None, speed, rpm, gear, thr, brk,
    car_ms, 0]`-shaped row for the CarData entry's own timestamp."""

    def test_car_data_with_no_eligible_position_still_persists_a_null_dp_sample_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        # No position has ever been buffered for this driver -> no eligible match.
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 250, "0": 11000, "3": 4, "4": 60, "5": 0}}}}]},
            _t(0),
        )
        self.assertEqual(
            len(drv.samples), 1,
            "a CarData entry with no eligible (preceding, unconsumed) position "
            "must still persist as a dp=None sample under AC-3's emit-unpaired "
            "rule, not be silently skipped",
        )
        if drv.samples:
            self.assertIsNone(drv.samples[0][0], "the persisted sample's dp must be None")
            self.assertEqual(drv.samples[0][1], 250, "channel data (speed) must still be captured")


class WrapDetectionStillWorksWithBuffer(unittest.TestCase):
    def test_sf_wrap_still_recorded_as_a_crossing(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        proc._handle_position({NUM: [0, 0, 95.0]}, _t(0))
        self.assertEqual(len(drv.crossings), 0)
        proc._handle_position({NUM: [0, 0, 5.0]}, _t(1))   # 95 -> 5 = S/F wrap
        self.assertEqual(len(drv.crossings), 1)
        # Both the pre-wrap and post-wrap position were buffered (pairing candidates).
        self.assertEqual(len(drv.pos_buffer), 2)


if __name__ == "__main__":
    unittest.main()
