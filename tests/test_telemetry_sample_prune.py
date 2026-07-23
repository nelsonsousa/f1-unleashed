"""4tgWbUfx — TelemetryProcessor must prune DriverData.samples down to the open
lap (plus a small pre-seam bracket) once laps commit, so the list and the
per-commit filters stay bounded instead of growing O(session length).
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.processors.telemetry_processor import (
    TelemetryProcessor, DriverData, _epoch_ms,
)

BASE = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)


def _ms(sec: float) -> int:
    return int((BASE + timedelta(seconds=sec)).timestamp() * 1000)


def _samples(count: int, step_s: float = 1.0) -> list:
    # sample tuple: [dp, speed, rpm, gear, thr, brk, abs_ms, est]; s[6] = abs_ms
    return [[0.0, 100, 0, 5, 50, 0, _ms(i * step_s), 0] for i in range(count)]


class PruneSamples(unittest.TestCase):
    def setUp(self):
        self.p = TelemetryProcessor.__new__(TelemetryProcessor)   # no bus needed

    def test_prunes_to_open_lap_minus_bracket(self):
        drv = DriverData(num="44")
        drv.samples = _samples(120)                 # 120 samples over 120 s
        open_start = BASE + timedelta(seconds=100)  # current open lap starts at t=100 s
        drv.crossings = [open_start]

        self.p._prune_samples(drv)

        cutoff = _epoch_ms(open_start) - TelemetryProcessor._SAMPLE_BRACKET_MS
        self.assertTrue(drv.samples, "must not drop everything")
        # Nothing older than (open lap start - bracket) survives.
        self.assertTrue(all(s[6] >= cutoff for s in drv.samples))
        # The pre-seam bracket sample (just before the open lap start) is retained
        # for _synthetic_at_seam.
        self.assertTrue(any(s[6] < _epoch_ms(open_start) for s in drv.samples))
        # And it actually pruned (the ~95 committed samples before the bracket).
        self.assertLess(len(drv.samples), 120)
        # Bracket is ~5 s of samples at 1/s → ~5-6 kept before the seam + 20 after.
        self.assertLessEqual(len(drv.samples), 30)

    def test_noop_without_open_lap(self):
        # No open lap boundary (e.g. after STOP) → nothing to prune against; keep all.
        drv = DriverData(num="44")
        drv.samples = _samples(10)
        drv.crossings = []
        self.p._prune_samples(drv)
        self.assertEqual(len(drv.samples), 10)

    def test_noop_when_all_samples_already_within_open_lap(self):
        drv = DriverData(num="44")
        drv.samples = _samples(10)                  # t = 0..9 s
        drv.crossings = [BASE]                       # open lap starts at t=0
        before = list(drv.samples)
        self.p._prune_samples(drv)
        self.assertEqual(drv.samples, before)       # nothing older than cutoff


if __name__ == "__main__":
    unittest.main()
