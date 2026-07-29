"""Coverage-closing tests for `_emit_lap`'s seam-stripping loops
(`telemetry_processor.py`, requirement-spec.md AC-3's None-dp guard).

The AC-3 null-dp crash-fix tests (`tests/regression/test_telemetry_null_dp_
crash_ac3.py`) exercise the "one side of the comparison is None, loop body
never runs" guard case, but never a scenario where BOTH sides are real
(non-None) numbers and are genuinely out of order — the case the seam-strip
loops actually exist to handle, and the branch direction the None-guard's
`and` conditions gate on when the guard itself does not apply. Closes the
must-fix 3 (2026-07-29 fix-attempt 2) branch-coverage gap on
`telemetry_processor.py` lines ~721/724 (the `while` loops' actual pop
branch, for both the leading and trailing loop).
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


def _sample(dp, t_ms, speed=200, est=0):
    return [dp, speed, 10000, 3, 50, 0, t_ms, est]


class LeadingSeamStripPopsOnGenuineNonNullOutOfOrderPair(unittest.TestCase):
    """A previous-lap straggler (dp near 100) at the front of the window,
    both it and the next sample non-null, genuinely out of dp order
    (`in_lap[0][0] > in_lap[1][0]`) — the actual pop branch, not the
    None-guard's skip branch."""

    def test_leading_straggler_with_both_sides_non_null_is_stripped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(95.0, _epoch_ms(_t(0.1))),   # straggler: dp descends into sample 2
            _sample(5.0, _epoch_ms(_t(1.0))),
            _sample(10.0, _epoch_ms(_t(2.0))),
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        self.assertEqual(topic, f"telemetryLap:{NUM}:1")
        dp_values = [row[0] for row in out]
        self.assertNotIn(
            95.0, dp_values,
            "the leading straggler (both sides non-null, genuinely "
            "out-of-order) must be popped by the seam-strip loop",
        )
        self.assertIn(5.0, dp_values)
        self.assertIn(10.0, dp_values)


class TrailingSeamStripPopsOnGenuineNonNullOutOfOrderPair(unittest.TestCase):
    """A next-lap straggler (dp near 0) at the back of the window, both it
    and the preceding sample non-null, genuinely out of dp order
    (`in_lap[-1][0] < in_lap[-2][0]`)."""

    def test_trailing_straggler_with_both_sides_non_null_is_stripped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(50.0, _epoch_ms(_t(1.0))),
            _sample(95.0, _epoch_ms(_t(2.0))),
            _sample(5.0, _epoch_ms(_t(2.9))),    # straggler: dp drops back below sample 2
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        dp_values = [row[0] for row in out]
        self.assertNotIn(
            5.0, dp_values,
            "the trailing straggler (both sides non-null, genuinely "
            "out-of-order) must be popped by the seam-strip loop",
        )
        self.assertIn(50.0, dp_values)
        self.assertIn(95.0, dp_values)


class SeamStripSkipsOverNullDpBoundaryInsteadOfHalting(unittest.TestCase):
    """Should-fix 4 (2026-07-29 fix-attempt 2): a null-dp row immediately
    adjacent to a genuine straggler must no longer defeat the strip —
    previously the loop halted entirely the moment EITHER compared side was
    None, so a straggler separated from its comparison partner by a
    null-dp row survived in the trace at the measured ~18-20% null rate."""

    def test_leading_straggler_separated_from_comparator_by_a_null_dp_row_is_still_stripped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(95.0, _epoch_ms(_t(0.1))),   # genuine straggler
            _sample(None, _epoch_ms(_t(0.5))),   # null-dp row directly between them
            _sample(5.0, _epoch_ms(_t(1.0))),
            _sample(10.0, _epoch_ms(_t(2.0))),
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        dp_values = [row[0] for row in out]
        self.assertNotIn(
            95.0, dp_values,
            "a straggler separated from its comparison partner by a "
            "null-dp row must still be stripped, not left in the trace "
            "because the old adjacent-pair comparison halted on the null",
        )
        # The null-dp row itself is retained (never popped) as a legitimate
        # gap sample.
        self.assertIn(None, dp_values)
        self.assertIn(5.0, dp_values)
        self.assertIn(10.0, dp_values)

    def test_trailing_straggler_separated_from_comparator_by_a_null_dp_row_is_still_stripped(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(50.0, _epoch_ms(_t(1.0))),
            _sample(95.0, _epoch_ms(_t(2.0))),
            _sample(None, _epoch_ms(_t(2.5))),   # null-dp row directly between them
            _sample(5.0, _epoch_ms(_t(2.9))),    # genuine straggler
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        dp_values = [row[0] for row in out]
        self.assertNotIn(
            5.0, dp_values,
            "a trailing straggler separated from its comparison partner by "
            "a null-dp row must still be stripped",
        )
        self.assertIn(None, dp_values)
        self.assertIn(50.0, dp_values)
        self.assertIn(95.0, dp_values)


if __name__ == "__main__":
    unittest.main()
