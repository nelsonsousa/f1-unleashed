"""AC-7 RED GATE (requirement-spec.md AC-7, docs/artifacts/
2026-08-01-040-merged-position-telemetry-processor/requirement-spec.md):

"A CarData stream stall longer than the current MAX_DT_S (2.0s) does not
silently discard the distance covered during the stall. Either the reckoner
keeps integrating across the stall (removing/raising the hard clamp), or the
stall is explicitly detected and the reckoned dp for that span is flagged
invalid/forced to resync on the next real signal -- but under no
circumstance does the merged unit reproduce today's behavior of clamping
`dt` and silently under-integrating."

`position_processor.py`'s dead-reckoning integration step is:

    dt = (clock_time - self._r_ts[num]).total_seconds()
    self._r_ts[num] = clock_time
    ...
    dt = min(dt, MAX_DT_S)              # <-- the clamp this test targets
    ...
    ddp = self._C * speed * dt

`MAX_DT_S = 2.0`. When two consecutive CarData samples for a car are more
than 2.0s apart (a feed stall) while the car is moving, the elapsed wall-
clock gap is silently truncated to 2.0s before being multiplied by speed --
the distance the car actually covered during the truncated portion of the
stall is discarded with no trace (no flag, no counter, nothing downstream
can tell it happened). This is exactly the "silently under-integrate" defect
AC-7 requires be gone.

This test drives `PositionProcessor._handle_car_data` directly (the same
white-box technique `test_ac4_latch_ordering_defects.py` and
`test_position_processor_sc_active_lookup_coverage.py` already use to reach
the reckoning code without needing a real SignalR/StreamNormalizer feed or a
real track SVG), pre-seeding calibration/reckoner state so the calibration
bootstrap (`_C is None or _cal_n < 30`) is not itself what's under test.

Written from requirement-spec.md AC-7 alone. No proposed fix has been read
or anticipated -- this is the Red Gate: it must be observed FAILING against
today's unfixed `position_processor.py` (the `dt = min(dt, MAX_DT_S)` line
is present, unmodified) before any implementation work begins.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

from app.processing.processors.position_processor import PositionProcessor, MAX_DT_S
from app.processing.track_geometry import TrackGeometry

NUM = "44"
SPEED_KPH = 300.0          # a known, non-trivial, constant speed through the stall
C = 0.01                   # dp% per (kph * s) -- a stand-in calibrated conversion factor


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _degenerate_geo(total_dist: float = 100.0) -> TrackGeometry:
    """A minimal single-segment "track" -- enough for `_dist_pct_to_xy`'s
    searchsorted/interpolation math to run without a real SVG (same
    construction the existing position_processor coverage/red-gate tests
    use)."""
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


def _car_data(speed_kph: float) -> dict:
    return {"Entries": [{"Cars": {NUM: {"Channels": {
        "2": speed_kph,   # speed (km/h)
        "4": 100,         # throttle % -- steady full throttle, no braking, so
        "5": 0,           # _smooth_speed's impossible-blip filter never engages
    }}}}]}


def _seeded_proc(*, start_dp: float = 0.0, start_ts: datetime) -> PositionProcessor:
    """A PositionProcessor with calibration and reckoner state already
    established for car NUM as of `start_ts` -- isolates the MAX_DT_S clamp
    itself as the thing under test, the same seeding pattern
    `test_position_processor_sc_active_lookup_coverage.py` uses to bypass
    the calibration bootstrap (`_C is None or _cal_n < 30`)."""
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    proc = PositionProcessor(bus, "race")
    proc._geo = _degenerate_geo()
    proc._last_pos_ts[NUM] = start_ts      # "had a real fix" -- eligible to reckon
    proc._r_ts[NUM] = start_ts
    proc._r_dp[NUM] = start_dp
    proc._C = C
    proc._cal_n = 100                       # well past the 30-interval bootstrap
    proc._miss[NUM] = 0
    return proc


class MaxDtClampSilentlyDiscardsDistanceDuringStall_RedGate(unittest.TestCase):
    def test_stall_longer_than_max_dt_s_integrates_full_elapsed_time_not_clamped(self):
        """A car moving at a known, constant, non-trivial speed (300 km/h) whose
        CarData feed stalls for 5.0s (more than double MAX_DT_S=2.0) must have
        its dead-reckoned dp advanced by the distance covered over the FULL
        5.0s gap, computed from the sample's own elapsed wall-clock time -- not
        silently truncated to a 2.0s-equivalent distance.
        """
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))
        stall_s = 5.0
        self.assertGreater(stall_s, MAX_DT_S, "test setup: the stall must exceed MAX_DT_S")

        proc._handle_car_data(_car_data(SPEED_KPH), _t(stall_s))

        expected_dp = C * SPEED_KPH * stall_s     # 0.01 * 300 * 5.0 = 15.0
        actual_dp = proc._r_dp[NUM]

        # Today's code computes `dt = min(dt, MAX_DT_S)` first, so the actual
        # result is C * SPEED_KPH * MAX_DT_S = 6.0 -- discarding the distance
        # covered by the remaining (5.0 - 2.0) = 3.0s of the stall entirely,
        # with nothing downstream able to detect it happened.
        discarded_dp = expected_dp - actual_dp
        self.assertAlmostEqual(
            actual_dp, expected_dp, places=6,
            msg=(
                f"AC-7 violated: a {stall_s}s CarData stall at {SPEED_KPH} km/h should "
                f"advance dp by {expected_dp:.3f}% (the car's full elapsed distance), but "
                f"the reckoner silently discarded {discarded_dp:.3f}% of that -- consistent "
                f"with today's `dt = min(dt, MAX_DT_S)` clamp truncating the {stall_s}s gap "
                f"down to MAX_DT_S={MAX_DT_S}s before integrating."
            ),
        )

    def test_stall_shorter_than_max_dt_s_is_unaffected(self):
        """Control case: a gap SHORTER than MAX_DT_S is never touched by the
        clamp either way, so it must integrate correctly both before and
        after any fix -- this pins down that the defect is specifically about
        stalls EXCEEDING MAX_DT_S, not the reckoner's integration in general.
        """
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))
        short_gap_s = 1.0
        self.assertLess(short_gap_s, MAX_DT_S)

        proc._handle_car_data(_car_data(SPEED_KPH), _t(short_gap_s))

        expected_dp = C * SPEED_KPH * short_gap_s   # 0.01 * 300 * 1.0 = 3.0
        self.assertAlmostEqual(proc._r_dp[NUM], expected_dp, places=6)


if __name__ == "__main__":
    unittest.main()
