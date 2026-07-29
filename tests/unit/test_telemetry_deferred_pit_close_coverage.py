"""Coverage-closing tests for the AC-4 deferred PIT-close mechanism
(`TelemetryProcessor._maybe_close_pending_pit`, and its call sites in
`_handle_car_data`/`_handle_position`) — branches not exercised by
`tests/regression/test_ac4_latch_ordering_defects.py`'s red-gate test, which
drives the PIT event directly and never sends a subsequent CarData.z/
Position.z sample through the real handlers (see implementation-plan.md for
why that test cannot, as written, drive this far — a genuine test-plan
defect, not a gap in this implementation).

These tests exercise the real production path: a PIT event defers the
close, and a SUBSEQUENT CarData.z or Position.z arrival (via the actual
`_handle_car_data`/`_handle_position` handlers) triggers it once caught up.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.telemetry_processor import TelemetryProcessor

NUM = "1"


def _proc() -> TelemetryProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return TelemetryProcessor(bus, "practice")


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class NoPendingCloseIsANoOp(unittest.TestCase):
    def test_calling_with_no_pending_close_does_nothing(self):
        proc = _proc()
        drv = proc._drv(NUM)
        self.assertIsNone(drv.pending_pit_close_ts)
        proc._maybe_close_pending_pit(drv, _t(5.0))   # must not raise
        self.assertIsNone(drv.pending_pit_close_ts)


class DeferredCloseTriggeredByCarData(unittest.TestCase):
    def test_car_data_before_pit_timestamp_does_not_trigger_close_yet(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        drv.crossings = [_t(0.0)]
        drv.cur_lap = 1
        proc._handle_wildcard(f"driverStatus:{NUM}", "PIT", _t(5.0))
        self.assertEqual(drv.pending_pit_close_ts, _t(5.0))

        # A CarData entry whose OWN timestamp is still BEFORE the PIT's own
        # timestamp -- must not trigger the close yet (not caught up).
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 200}}}}]}, _t(2.0))
        self.assertEqual(drv.pending_pit_close_ts, _t(5.0), "close must stay deferred")
        emitted = [c for c in proc._bus.emit.call_args_list if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(len(emitted), 0, "no telemetryLap emit yet -- close hasn't fired")

    def test_car_data_at_or_after_pit_timestamp_triggers_the_deferred_close(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        drv.crossings = [_t(0.0)]
        drv.cur_lap = 1
        proc._handle_wildcard(f"driverStatus:{NUM}", "PIT", _t(5.0))
        self.assertEqual(drv.pending_pit_close_ts, _t(5.0))

        # Caught up: a CarData entry whose own timestamp is >= the PIT's.
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {"Channels": {"2": 200}}}}]}, _t(5.0))
        self.assertIsNone(drv.pending_pit_close_ts, "close must have fired and cleared")
        emitted = [c for c in proc._bus.emit.call_args_list if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(len(emitted), 1)


class DeferredCloseTriggeredByPosition(unittest.TestCase):
    def test_position_arrival_also_advances_and_triggers_the_deferred_close(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        drv.crossings = [_t(0.0)]
        drv.cur_lap = 1
        proc._handle_wildcard(f"driverStatus:{NUM}", "PIT", _t(5.0))

        # A Position.z arrival, own timestamp >= PIT's -- doesn't itself add
        # a drv.samples row, but its timestamp having caught up is enough to
        # release the deferred close (shared watermark reasoning).
        proc._handle_position({NUM: [0, 0, 10.0]}, _t(6.0))
        self.assertIsNone(drv.pending_pit_close_ts)
        emitted = [c for c in proc._bus.emit.call_args_list if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(len(emitted), 1)


if __name__ == "__main__":
    unittest.main()
