"""AC-3 crash-risk RED GATE (code-mapper finding, file-impact-map.md §3/§6):

Once Candidate A's emit-unpaired-after-timeout lands, `TelemetryProcessor.
drv.samples` can legitimately contain a row with `dp=None` (a CarData entry
that found no eligible position within the pairing window). Two call sites
perform arithmetic/comparison directly on `dp` (`sample[0]`) with no
None-guard and will raise `TypeError` the first time a `None` reaches them:

  - `_emit_lap` (telemetry_processor.py ~650-653): the seam-stripping loop
    compares `in_lap[0][0] > in_lap[1][0]` / `in_lap[-1][0] < in_lap[-2][0]`.
  - `_synthetic_at_seam` (telemetry_processor.py ~728): `gap = (100.0 -
    before[0]) + after[0]`.

THIS IS THE RED GATE, not a proposed fix: today (no null dp ever reaches
`drv.samples` — `_handle_car_data`'s `if match is None: continue` guarantees
it, see telemetry_processor.py:539-541) these two functions have simply never
been exercised with a None-dp sample. Since nothing in the current pipeline
can *produce* one, the only way to prove they are not null-safe TODAY is to
call them directly with a hand-constructed None-dp sample (matching exactly
the row shape `_handle_car_data` will start emitting under AC-3) and observe
the TypeError. This is a direct reproduction of the "crash on first null
sample" defect the fix must close — no proposed fix has been read to write
this test (rules/testing.md, "The Red Gate").

Two complementary test shapes are used, both against today's unfixed code:

  1. `*_RedGate` classes below: `assertRaises(TypeError)` — a direct, currently
     PASSING proof that the crash exists today (evidence for the failure
     mode itself, captured verbatim in test-plan.md).
  2. `*_ExpectedFixedBehavior` classes: assert the CORRECT/fixed behavior
     (no exception; the None-dp row is excluded from seam arithmetic but
     retained in the emitted trace as a legitimate "channels present,
     position unknown" gap row, per file-impact-map.md §3's own
     recommendation). These are the standard red-gate shape (rules/testing.md)
     — written against the acceptance bar, currently FAILING (erroring) —
     and are what the fix must make pass; the `*_RedGate` classes above do
     not themselves flip to passing/failing the same way (they assert
     `assertRaises`, which correctly starts FAILING once the fix lands —
     that is by design, not a bug in the test). Marked
     `@unittest.expectedFailure` (2026-07-29, fix-attempt 2, should-fix 5) so
     the suite doesn't show permanent red for tests that did their job —
     their red-gate evidence is already captured verbatim in test-plan.md;
     an expected failure that starts passing again (XPASS) would mean the
     crash regressed.
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


# Sample tuple shape (telemetry_processor.py module docstring):
# [dp, speed, rpm, gear, throttle, brake, t_ms_rel-or-abs_ms, est]
def _sample(dp, t_ms, speed=200, est=0):
    return [dp, speed, 10000, 3, 50, 0, t_ms, est]


class EmitLapCrashesOnNullDpAtSeam_RedGate(unittest.TestCase):
    """`_emit_lap`'s seam-stripping loop: a None-dp row at either end of the
    windowed sample list raises TypeError comparing None to a float."""

    @unittest.expectedFailure
    def test_null_dp_leading_sample_crashes_emit_lap_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start = _t(0)
        end = _t(10)
        # A None-dp CarData-only sample (AC-3's new unpaired-emit case) at the
        # very front of the window, followed by a normal descending-then-
        # ascending dp shape that would otherwise trigger the seam-strip
        # comparison `in_lap[0][0] > in_lap[1][0]`.
        drv.samples = [
            _sample(None, _epoch_ms(_t(0.5))),
            _sample(5.0, _epoch_ms(_t(1.0))),
            _sample(10.0, _epoch_ms(_t(2.0))),
        ]
        with self.assertRaises(TypeError):
            proc._emit_lap(drv, 1, start, end)

    @unittest.expectedFailure
    def test_null_dp_trailing_sample_crashes_emit_lap_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start = _t(0)
        end = _t(10)
        drv.samples = [
            _sample(80.0, _epoch_ms(_t(1.0))),
            _sample(90.0, _epoch_ms(_t(2.0))),
            _sample(None, _epoch_ms(_t(3.0))),
        ]
        with self.assertRaises(TypeError):
            proc._emit_lap(drv, 1, start, end)


class SyntheticAtSeamCrashesOnNullDpBracket_RedGate(unittest.TestCase):
    """`_synthetic_at_seam`: `gap = (100.0 - before[0]) + after[0]` raises
    TypeError if either bracketing sample's dp is None."""

    @unittest.expectedFailure
    def test_null_dp_before_seam_crashes_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        seam_ts = _t(5.0)
        drv.samples = [
            _sample(None, _epoch_ms(_t(4.5))),   # brackets BEFORE the seam
            _sample(2.0, _epoch_ms(_t(5.5))),     # brackets AFTER the seam
        ]
        with self.assertRaises(TypeError):
            proc._synthetic_at_seam(drv, seam_ts, 0.0)

    @unittest.expectedFailure
    def test_null_dp_after_seam_crashes_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        seam_ts = _t(5.0)
        drv.samples = [
            _sample(98.0, _epoch_ms(_t(4.5))),
            _sample(None, _epoch_ms(_t(5.5))),
        ]
        with self.assertRaises(TypeError):
            proc._synthetic_at_seam(drv, seam_ts, 0.0)


class EndToEndBuildFailingCrash_RedGate(unittest.TestCase):
    """The build-failing consequence described in file-impact-map.md §3: an
    uncaught TypeError from `_emit_lap` during a normal lap-close call chain
    (`_try_close` -> `_emit_lap`), not just a direct unit call. Demonstrates
    the crash reaches the same call path production lap-closing uses."""

    @unittest.expectedFailure
    def test_lap_close_via_try_close_crashes_on_null_dp_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        drv.crossings = [_t(0), _t(10)]   # one bounded lap window
        drv.committed = 0
        # A None-dp sample inside the lap window — exactly what AC-3's
        # emit-unpaired persistence will start producing.
        drv.samples = [
            _sample(None, _epoch_ms(_t(1.0))),
            _sample(20.0, _epoch_ms(_t(2.0))),
            _sample(50.0, _epoch_ms(_t(5.0))),
        ]
        with self.assertRaises(TypeError):
            proc._try_close(drv, 1, _t(10))


class EmitLapExpectedFixedBehavior_RedGate(unittest.TestCase):
    """Standard red-gate shape: assert the CORRECT/fixed behavior directly
    (no exception; None-dp rows excluded from seam-stripping arithmetic but
    retained in the emitted trace) and observe it currently FAIL (errors with
    TypeError) against unfixed code."""

    def test_null_dp_leading_sample_does_not_crash_and_is_retained_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(None, _epoch_ms(_t(0.5))),
            _sample(5.0, _epoch_ms(_t(1.0))),
            _sample(10.0, _epoch_ms(_t(2.0))),
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        self.assertEqual(len(proc._bus.emit.call_args_list), 1)
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        self.assertEqual(topic, f"telemetryLap:{NUM}:1")
        # The None-dp row must still be present in the emitted trace (a
        # legitimate "channels present, position unknown" gap row), not
        # silently dropped.
        dp_values = [row[0] for row in out]
        self.assertIn(None, dp_values, "a None-dp sample must survive into the emitted trace as a gap row")

    def test_null_dp_trailing_sample_does_not_crash_and_is_retained_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start, end = _t(0), _t(10)
        drv.samples = [
            _sample(80.0, _epoch_ms(_t(1.0))),
            _sample(90.0, _epoch_ms(_t(2.0))),
            _sample(None, _epoch_ms(_t(3.0))),
        ]
        proc._emit_lap(drv, 1, start, end)   # must not raise
        topic, out, _ts = proc._bus.emit.call_args_list[0].args
        dp_values = [row[0] for row in out]
        self.assertIn(None, dp_values)


class SyntheticAtSeamExpectedFixedBehavior_RedGate(unittest.TestCase):
    def test_null_dp_bracket_does_not_crash_RED_GATE(self):
        proc = _proc()
        drv = proc._drv(NUM)
        drv.activated = True
        seam_ts = _t(5.0)
        drv.samples = [
            _sample(None, _epoch_ms(_t(4.5))),
            _sample(2.0, _epoch_ms(_t(5.5))),
        ]
        # Must not raise; a None-dp bracket makes the seam sample
        # unconstructible (no reliable gap to interpolate across), so the
        # fixed behavior is to skip the synthetic seam sample (return None)
        # rather than crash.
        result = proc._synthetic_at_seam(drv, seam_ts, 0.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
