"""Coverage-closing tests for `PositionProcessor._sc_active_at` (AC-4,
requirement-spec.md) branches not exercised by
`tests/regression/test_ac4_latch_ordering_defects.py`'s red-gate test (which
only drives the "sample precedes every transition" / break branch): a
sample timestamped AT-OR-AFTER a transition (the `active = value` branch),
and multiple transitions where the correct one must be picked.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

from app.processing.processors.position_processor import PositionProcessor
from app.processing.track_geometry import TrackGeometry


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _proc() -> PositionProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return PositionProcessor(bus, "race")


def _degenerate_geo(total_dist: float = 100.0) -> TrackGeometry:
    """A minimal single-segment "track" — enough for `_dist_pct_to_xy`'s
    searchsorted/interpolation math to run without a real SVG (same
    construction `tests/regression/test_ac4_latch_ordering_defects.py`
    uses)."""
    return TrackGeometry(
        points=[(0.0, 0.0), (total_dist, 0.0)],
        seg_starts=np.array([[0.0, 0.0]]),
        seg_dirs=np.array([[total_dist, 0.0]]),
        seg_len_sq=np.array([total_dist ** 2]),
        seg_len=np.array([total_dist]),
        seg_cum_dist=np.array([0.0]),
        total_dist=total_dist,
        sf_offset=0.0,
        lap_distance=total_dist,
    )


class ScActiveAtLookup(unittest.TestCase):
    def test_no_transitions_yet_defaults_to_false(self):
        proc = _proc()
        self.assertFalse(proc._sc_active_at(_t(0)))

    def test_sample_at_or_after_a_transition_reads_that_transitions_value(self):
        proc = _proc()
        proc._handle_track_status({"Status": "4"}, _t(5.0))   # SC active from t=5
        self.assertTrue(proc._sc_active_at(_t(5.0)))   # exactly at
        self.assertTrue(proc._sc_active_at(_t(9.0)))   # after

    def test_multiple_transitions_picks_the_latest_one_not_exceeding_ts(self):
        proc = _proc()
        proc._handle_track_status({"Status": "4"}, _t(5.0))    # -> active
        proc._handle_track_status({"Status": "1"}, _t(10.0))   # -> green (inactive)
        proc._handle_track_status({"Status": "4"}, _t(20.0))   # -> active again
        self.assertTrue(proc._sc_active_at(_t(7.0)))     # between 5 and 10 -> active
        self.assertFalse(proc._sc_active_at(_t(15.0)))   # between 10 and 20 -> inactive
        self.assertTrue(proc._sc_active_at(_t(25.0)))    # after 20 -> active


class ScActiveTrueBranchReachedViaHandleCarData(unittest.TestCase):
    """Coverage gap closed 2026-07-29 (fix-attempt 2, must-fix 3): the
    `_sc_active_at` FUNCTION had both True/False branches covered above, but
    nothing drove the actual CALL SITE inside `_handle_car_data`
    (`if self._sc_active_at(clock_time):`) with a scenario where it returns
    True — every test reaching `_handle_car_data` (the red-gate test and the
    other coverage tests) did so with a sample predating the SC/VSC
    transition, i.e. only the False branch. This exercises the dead-reckon/
    clamp/no-apex-snap body (SC-active branch) via the real call site."""

    def _proc(self) -> PositionProcessor:
        proc = _proc()
        proc._geo = _degenerate_geo()
        proc._last_pos_ts["44"] = _t(0)
        proc._r_ts["44"] = _t(0)
        proc._r_dp["44"] = 50.0
        proc._C = 1.0
        proc._cal_n = 100
        proc._miss["44"] = 0
        return proc

    def test_car_data_sample_timestamped_after_sc_transition_takes_the_sc_active_branch(self):
        proc = self._proc()
        # SC/VSC active from t=0 (before the CarData sample below).
        proc._handle_track_status({"Status": "4"}, _t(0.0))
        self.assertTrue(proc._sc_active_at(_t(1.0)))

        car_data = {"Entries": [{"Cars": {"44": {"Channels": {"2": 100}}}}]}
        proc._handle_car_data(car_data, _t(1.0))

        # SC-active branch: dp is dead-reckoned and clamped below 100, and no
        # natural wrap is recorded (`_wrapped` stays unset) — the observable
        # divergence from the non-SC branch the sibling red-gate test in
        # `test_ac4_latch_ordering_defects.py` already relies on.
        self.assertNotIn("44", proc._wrapped)
        self.assertLess(proc._r_dp["44"], 100.0)


if __name__ == "__main__":
    unittest.main()
