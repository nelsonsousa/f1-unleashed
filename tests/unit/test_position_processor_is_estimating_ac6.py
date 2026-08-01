"""AC-6/AC-1/AC-5/AC-8/AC-9/AC-12 formal test suite for WB2
(`docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/`
implementation-plan.md, "WB2: PositionProcessor integration"):
`PositionProcessor._is_estimating()` and its three call sites, replacing the
old `MISS_SAMPLES` sample-count gate with a wall-clock-elapsed-time gate.

Written from `requirement-spec.md` (AC-1, AC-5, AC-6, AC-8, AC-9, AC-12) and
a direct read of the shipped diff (`position_processor.py`, post-WB2) -- not
from the implementer's manual-sanity-script summary in implementation-plan.md
§4 step 7, which this suite exists to make durable and independently
re-verifiable.

The three call sites replaced (confirmed by reading the diff, not assumed):
  1. `_handle_car_data`'s SC/VSC branch (`if not self._is_estimating(...)`)
  2. `_handle_car_data`'s normal branch (same condition)
  3. `_handle_wildcard`'s synthetic S/F-completion gate
     (`... and self._is_estimating(num, clock_time)`)

Boundary semantics (confirmed by reading `_is_estimating` itself): the
comparison is `(clock_time - last_ts).total_seconds() > EST_THRESHOLD_S`
(strict `>`). A gap of EXACTLY EST_THRESHOLD_S is therefore still
real/tolerated (not estimating); only a gap strictly GREATER than it flips
to estimating.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

from app.processing.processors.position_processor import (
    PositionProcessor, EST_THRESHOLD_S,
)
from app.processing.track_geometry import TrackGeometry

NUM = "44"
SPEED_KPH = 300.0
C = 0.01   # dp% per (kph*s) -- same stand-in factor test_position_processor_max_dt_stall_discard_ac7.py uses


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _degenerate_geo(total_dist: float = 100.0) -> TrackGeometry:
    """A minimal single-segment "track" -- enough for `_dist_pct_to_xy`'s
    searchsorted/interpolation math (and `project_local`'s projection) to
    run without a real SVG. Same construction the sibling
    position_processor tests use."""
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


def _mock_bus() -> mock.Mock:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    return bus


def _proc() -> PositionProcessor:
    return PositionProcessor(_mock_bus(), "race")


def _car_data(speed_kph: float) -> dict:
    return {"Entries": [{"Cars": {NUM: {"Channels": {
        "2": speed_kph,   # speed (km/h)
        "4": 100,         # throttle % -- full throttle, no brake, so
        "5": 0,           # _smooth_speed's impossible-blip filter never engages
    }}}}]}


def _seeded_proc(*, start_dp: float = 0.0, start_ts: datetime) -> PositionProcessor:
    """A PositionProcessor with calibration and reckoner state already
    established for car NUM as of `start_ts` -- the same seeding pattern
    `test_position_processor_max_dt_stall_discard_ac7.py` and
    `test_position_processor_sc_active_lookup_coverage.py` use to bypass the
    calibration bootstrap (`_C is None or _cal_n < MIN_CAL_N`)."""
    proc = _proc()
    proc._geo = _degenerate_geo()
    proc._last_pos_ts[NUM] = start_ts
    proc._r_ts[NUM] = start_ts
    proc._r_dp[NUM] = start_dp
    proc._C = C
    proc._cal_n = 100
    proc._miss[NUM] = 0
    return proc


def _position_calls(bus: mock.Mock) -> list:
    return [c for c in bus.emit.call_args_list if c.args[0] == "position"]


# ─────────────────────────────────────────────────────────────────────────
# `_is_estimating` itself: the boundary, in isolation from any call site.
# ─────────────────────────────────────────────────────────────────────────
class IsEstimatingBoundary(unittest.TestCase):
    def test_never_had_a_real_fix_is_not_estimating(self):
        proc = _proc()
        self.assertFalse(proc._is_estimating(NUM, _t(1000.0)))

    def test_gap_strictly_under_threshold_is_not_estimating(self):
        proc = _proc()
        proc._last_pos_ts[NUM] = _t(0.0)
        self.assertFalse(proc._is_estimating(NUM, _t(EST_THRESHOLD_S - 0.001)))

    def test_gap_exactly_at_threshold_is_not_estimating(self):
        """The comparison is strict `>`, so exactly EST_THRESHOLD_S elapsed
        is still real/tolerated -- this pins down the `>` vs `>=` boundary
        choice `implementation-plan.md` §5 ("Threshold sensitivity at
        exactly 1.0s") flags explicitly."""
        proc = _proc()
        proc._last_pos_ts[NUM] = _t(0.0)
        self.assertFalse(proc._is_estimating(NUM, _t(EST_THRESHOLD_S)))

    def test_gap_just_over_threshold_is_estimating(self):
        proc = _proc()
        proc._last_pos_ts[NUM] = _t(0.0)
        self.assertTrue(proc._is_estimating(NUM, _t(EST_THRESHOLD_S + 0.001)))

    def test_gap_well_over_threshold_is_estimating(self):
        proc = _proc()
        proc._last_pos_ts[NUM] = _t(0.0)
        self.assertTrue(proc._is_estimating(NUM, _t(60.0)))


# ─────────────────────────────────────────────────────────────────────────
# Call site 1: `_handle_car_data`'s NORMAL (non-SC/VSC) branch.
# ─────────────────────────────────────────────────────────────────────────
class NormalBranchGateAC6(unittest.TestCase):
    def test_gap_just_under_threshold_stays_buffered_not_emitted(self):
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))
        proc._handle_car_data(_car_data(SPEED_KPH), _t(EST_THRESHOLD_S - 0.1))

        self.assertIn(NUM, proc._r_buf, "sub-threshold gap must be tolerated (buffered), not emitted")
        self.assertEqual(len(proc._r_buf[NUM]), 1)
        self.assertEqual(_position_calls(proc._bus), [], "no position emit while still within the threshold")

    def test_gap_exactly_at_threshold_stays_buffered_not_emitted(self):
        """Boundary at the actual call site, not just the helper: `>` means
        an exact 1.0s gap is still tolerated."""
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))
        proc._handle_car_data(_car_data(SPEED_KPH), _t(EST_THRESHOLD_S))

        self.assertIn(NUM, proc._r_buf)
        self.assertEqual(_position_calls(proc._bus), [])

    def test_gap_just_over_threshold_switches_to_estimated_and_emits(self):
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))
        proc._handle_car_data(_car_data(SPEED_KPH), _t(EST_THRESHOLD_S + 0.1))

        self.assertNotIn(NUM, proc._r_buf, "once estimating, nothing should remain buffered for this car")
        calls = _position_calls(proc._bus)
        self.assertEqual(len(calls), 1)
        self.assertIn(NUM, calls[0].args[1])
        self.assertEqual(calls[0].args[1][NUM][3], 1, "entry must be flagged estimated (est=1)")

    def test_buffered_entry_is_backfilled_once_gate_flips(self):
        """Two ticks against the SAME (never-refreshed) last real fix: the
        first, sub-threshold tick is buffered; the second, over-threshold
        tick both emits its own estimated entry AND backfills the earlier
        buffered one at its OWN original timestamp -- proving the buffer
        isn't simply dropped when the gate flips."""
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))

        proc._handle_car_data(_car_data(SPEED_KPH), _t(0.5))     # buffered (0.5s < 1.0s)
        self.assertEqual(_position_calls(proc._bus), [])

        proc._handle_car_data(_car_data(SPEED_KPH), _t(1.5))     # 1.5s > 1.0s -> estimating
        calls = _position_calls(proc._bus)

        # One emit is the backfill of the t=0.5s buffered entry (at ITS OWN
        # timestamp), one is the current (t=1.5s) recon emit.
        self.assertEqual(len(calls), 2)
        backfill_call = next(c for c in calls if c.args[2] == _t(0.5))
        current_call = next(c for c in calls if c.args[2] == _t(1.5))
        self.assertIn(NUM, backfill_call.args[1])
        self.assertIn(NUM, current_call.args[1])
        self.assertNotIn(NUM, proc._r_buf, "buffer must be drained once backfilled")


# ─────────────────────────────────────────────────────────────────────────
# Call site 2: `_handle_car_data`'s SC/VSC branch.
# ─────────────────────────────────────────────────────────────────────────
class ScVscBranchGateAC6(unittest.TestCase):
    def _sc_seeded_proc(self, start_ts: datetime) -> PositionProcessor:
        proc = _seeded_proc(start_dp=0.0, start_ts=start_ts)
        proc._handle_track_status({"Status": "4"}, start_ts)   # SC active from the start
        return proc

    def test_gap_just_under_threshold_stays_buffered_not_emitted_under_sc(self):
        proc = self._sc_seeded_proc(_t(0.0))
        proc._handle_car_data(_car_data(SPEED_KPH), _t(EST_THRESHOLD_S - 0.1))

        self.assertIn(NUM, proc._r_buf)
        self.assertEqual(_position_calls(proc._bus), [])
        # SC-active branch clamps below 100 rather than free-wrapping -- confirm we're
        # actually inside the SC branch, not the normal one.
        self.assertLess(proc._r_dp[NUM], 100.0)

    def test_gap_just_over_threshold_switches_to_estimated_and_emits_under_sc(self):
        proc = self._sc_seeded_proc(_t(0.0))
        proc._handle_car_data(_car_data(SPEED_KPH), _t(EST_THRESHOLD_S + 0.1))

        self.assertNotIn(NUM, proc._r_buf)
        calls = _position_calls(proc._bus)
        self.assertEqual(len(calls), 1)
        self.assertIn(NUM, calls[0].args[1])
        self.assertEqual(calls[0].args[1][NUM][3], 1)


# ─────────────────────────────────────────────────────────────────────────
# Call site 3: `_handle_wildcard`'s synthetic S/F-completion gate.
# ─────────────────────────────────────────────────────────────────────────
class WildcardSyntheticCompletionGateAC6(unittest.TestCase):
    def _wc_proc(self, start_ts: datetime) -> PositionProcessor:
        proc = _proc()
        proc._geo = _degenerate_geo()
        proc._last_pos_ts[NUM] = start_ts
        proc._r_dp[NUM] = 50.0     # `num in self._r_dp` gate + a plausible mid-lap dp
        return proc

    def test_not_estimating_no_synthetic_completion_emitted(self):
        proc = self._wc_proc(_t(0.0))
        proc._handle_wildcard(
            f"driverLaps:{NUM}", {"currentLap": 2}, _t(EST_THRESHOLD_S - 0.1),
        )
        self.assertEqual(_position_calls(proc._bus), [],
                          "sub-threshold gap must not emit the synthetic 99.9% completion sample")
        self.assertEqual(proc._r_dp[NUM], 0.0, "S/F reset to 0 must still happen regardless")

    def test_estimating_synthetic_completion_is_emitted(self):
        proc = self._wc_proc(_t(0.0))
        proc._handle_wildcard(
            f"driverLaps:{NUM}", {"currentLap": 2}, _t(EST_THRESHOLD_S + 0.1),
        )
        calls = _position_calls(proc._bus)
        self.assertEqual(len(calls), 1)
        entry = calls[0].args[1][NUM]
        self.assertAlmostEqual(entry[2], 99.9)
        self.assertEqual(entry[3], 1)
        self.assertEqual(proc._r_dp[NUM], 0.0, "S/F reset to 0 happens after the synthetic emit")


# ─────────────────────────────────────────────────────────────────────────
# AC-1: `_handle_position` (the real-fix path) is unaffected by WB2.
# ─────────────────────────────────────────────────────────────────────────
class Ac1PositionHandlerNonRegression(unittest.TestCase):
    """No existing test drives `PositionProcessor._handle_position` itself
    with a real payload and asserts on its output (the pre-existing
    position_processor tests seed state directly and only exercise
    `_handle_car_data`/`_handle_wildcard`/`_handle_track_status`) -- this is
    not a duplicate of an existing test. Confirms the real-fix path (2.1)
    is byte-identical in behavior: a real (X, Y) fix projects onto the
    track, calibrates the reckoner, resets outage-tracking state, and emits
    on `position` with est=0 -- none of which routes through
    `_is_estimating` at all (confirmed by reading `_handle_position`: it
    contains no reference to `_is_estimating`/`EST_THRESHOLD_S`)."""

    def test_real_fix_projects_calibrates_and_emits_non_estimated(self):
        proc = _proc()
        proc._geo = _degenerate_geo()
        # Pre-existing outage-tracking state that a real fix must reset.
        proc._miss[NUM] = 7
        proc._r_buf[NUM] = [(_t(0.0), [1.0, 2.0, 3.0, 1])]

        proc._handle_position(
            {"Position": [{"Entries": {NUM: {"X": 50.0, "Y": 0.0}}}]}, _t(0.0),
        )

        self.assertEqual(proc._last_pos_ts[NUM], _t(0.0))
        self.assertEqual(proc._miss[NUM], 0, "real fix resets the diagnostic miss counter")
        self.assertNotIn(NUM, proc._r_buf, "real fix discards any tolerated buffer")

        calls = _position_calls(proc._bus)
        self.assertEqual(len(calls), 1)
        entry = calls[0].args[1][NUM]
        self.assertAlmostEqual(entry[0], 50.0)   # x
        self.assertAlmostEqual(entry[1], 0.0)    # y
        self.assertAlmostEqual(entry[2], 50.0)   # dist_pct on this degenerate 100-unit track
        self.assertEqual(len(entry), 3, "a REAL fix's emitted entry has no est flag element at all")

    def test_real_fix_seeds_reckoner_state_via_observe_real_position(self):
        proc = _proc()
        proc._geo = _degenerate_geo()

        proc._handle_position(
            {"Position": [{"Entries": {NUM: {"X": 25.0, "Y": 0.0}}}]}, _t(0.0),
        )

        self.assertTrue(proc._reckoner.is_seeded(NUM))
        self.assertAlmostEqual(proc._reckoner.current_dp(NUM), 25.0)


# ─────────────────────────────────────────────────────────────────────────
# AC-9: multiple independent short episodes for the same car -- estimating
# state clears on each real fix and re-triggers independently.
# ─────────────────────────────────────────────────────────────────────────
class Ac9EpisodeIndependence(unittest.TestCase):
    def test_two_independent_episodes_separated_by_a_real_fix(self):
        proc = _proc()
        proc._geo = _degenerate_geo()
        proc._C = C
        proc._cal_n = 100

        # Real fix #1, anchors the reckoner and starts episode 1's clock.
        proc._handle_position(
            {"Position": [{"Entries": {NUM: {"X": 10.0, "Y": 0.0}}}]}, _t(0.0),
        )
        self.assertFalse(proc._is_estimating(NUM, _t(0.0)))
        proc._bus.emit.reset_mock()   # drop the real-fix's own "position" emit before counting episode 1

        # Episode 1: gap grows past the threshold -> estimating, one emit.
        proc._handle_car_data(_car_data(SPEED_KPH), _t(1.5))
        self.assertTrue(proc._is_estimating(NUM, _t(1.5)))
        episode_1_calls = _position_calls(proc._bus)
        self.assertEqual(len(episode_1_calls), 1)

        # Real fix #2 recovers the car -- must immediately clear the
        # estimating state (no stuck/carried-over estimation).
        proc._handle_position(
            {"Position": [{"Entries": {NUM: {"X": 15.0, "Y": 0.0}}}]}, _t(1.6),
        )
        self.assertFalse(
            proc._is_estimating(NUM, _t(1.6)),
            "a fresh real fix must clear the estimating state immediately",
        )
        self.assertNotIn(NUM, proc._r_buf, "the real fix must discard episode 1's buffer")

        # Episode 2: a SECOND, independent gap, measured from the SECOND
        # real fix, not carried over from episode 1.
        proc._bus.emit.reset_mock()
        self.assertFalse(proc._is_estimating(NUM, _t(2.0)))    # 0.4s since fix #2: still real
        proc._handle_car_data(_car_data(SPEED_KPH), _t(2.0))
        self.assertEqual(_position_calls(proc._bus), [], "episode 2 has not started yet")

        proc._handle_car_data(_car_data(SPEED_KPH), _t(3.2))   # 1.6s since fix #2: estimating again
        self.assertTrue(proc._is_estimating(NUM, _t(3.2)))
        episode_2_calls = _position_calls(proc._bus)
        self.assertGreaterEqual(
            len(episode_2_calls), 1,
            "episode 2 must independently re-trigger estimation -- not stay stuck "
            "in whatever state episode 1 left, and not stay silently suppressed",
        )


# ─────────────────────────────────────────────────────────────────────────
# AC-8-shaped: a long stall -- the reckoner keeps integrating rather than
# resetting/stalling once the (much shorter) 1s gate has already flipped.
# ─────────────────────────────────────────────────────────────────────────
class Ac8LongStallKeepsIntegrating(unittest.TestCase):
    def test_dp_keeps_advancing_across_a_stall_far_longer_than_the_old_sample_gate(self):
        """The old MISS_SAMPLES=10 gate needed 10 CarData ticks (~2.4s at
        the ~240ms median rate) before switching to estimated, and nothing
        in that old design spoke to what happens across a much longer
        stall. Here the gate flips to estimating after a SINGLE tick past
        1.0s, and the reckoner (AC-7, WB1) keeps integrating the full
        elapsed distance on every subsequent tick for a 20s stall -- it is
        never reset, clamped, or silently stalled partway through."""
        proc = _seeded_proc(start_dp=0.0, start_ts=_t(0.0))

        # Uneven tick spacing (not a fixed sample rate) spanning a 20s stall,
        # with NO real fix in between -- deliberately exercises "the
        # reckoner keeps integrating" rather than "resets on a schedule".
        tick_times = [7.0, 14.0, 20.0]
        expected_dp = 0.0
        prev_t = 0.0
        for t in tick_times:
            proc._handle_car_data(_car_data(SPEED_KPH), _t(t))
            expected_dp += C * SPEED_KPH * (t - prev_t)
            prev_t = t
            self.assertTrue(
                proc._is_estimating(NUM, _t(t)),
                "the whole stall is well past the 1s threshold from the last real fix",
            )
            self.assertAlmostEqual(
                proc._r_dp[NUM], expected_dp, places=6,
                msg="dp must keep integrating the full elapsed distance across the "
                    "stall, not reset or stall out partway through it",
            )

        # 20s elapsed, 3 ticks -- far beyond what a 10-sample-count gate
        # would have required to even START estimating, and the reckoner
        # never stopped advancing throughout.
        self.assertAlmostEqual(proc._r_dp[NUM], C * SPEED_KPH * 20.0, places=6)
        self.assertEqual(proc._miss[NUM], len(tick_times),
                          "the diagnostic _miss counter still increments, but gates nothing")


# ─────────────────────────────────────────────────────────────────────────
# AC-12: determinism -- `_is_estimating` depends only on message timestamps.
# ─────────────────────────────────────────────────────────────────────────
class Ac12DeterminismNoWallClock(unittest.TestCase):
    def test_no_wall_clock_call_introduced_in_position_processor_source(self):
        """Static (AST-based, so docstrings/comments mentioning
        'datetime.now()' in prose -- e.g. this module's own outage-recovery
        docstring, which explicitly says it does NOT use it -- can't produce
        a false positive) check: no CALL to `datetime.now`, `.utcnow`, or
        `time.time` appears anywhere in the module. `_is_estimating` and its
        call sites must only ever compare against `clock_time`, the
        caller's own message timestamp."""
        import ast
        import inspect
        import app.processing.processors.position_processor as mod

        src = inspect.getsource(mod)
        tree = ast.parse(src)
        wall_clock_names = {"now", "utcnow", "time"}
        found = []

        class _Visitor(ast.NodeVisitor):
            def visit_Call(self, node):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None
                )
                if name in wall_clock_names:
                    found.append(name)
                self.generic_visit(node)

        _Visitor().visit(tree)
        self.assertEqual(found, [], f"wall-clock call(s) found: {found}")

    def test_is_estimating_is_correct_far_from_real_wall_clock_now(self):
        """A purely message-timestamp-driven computation must give the same
        answer regardless of how far the message timestamps are from the
        actual current wall-clock time -- proven here with timestamps in
        1999, nowhere near "now"."""
        proc = _proc()
        anchor = datetime(1999, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        proc._last_pos_ts[NUM] = anchor

        self.assertFalse(proc._is_estimating(NUM, anchor + timedelta(seconds=0.5)))
        self.assertFalse(proc._is_estimating(NUM, anchor + timedelta(seconds=EST_THRESHOLD_S)))
        self.assertTrue(proc._is_estimating(NUM, anchor + timedelta(seconds=EST_THRESHOLD_S + 0.001)))
        self.assertTrue(proc._is_estimating(NUM, anchor + timedelta(seconds=999.0)))

    def test_repeated_calls_with_identical_inputs_give_identical_results(self):
        """Same input sequence -> same output every time (no hidden
        state/randomness/order-dependence in the helper itself)."""
        proc = _proc()
        proc._last_pos_ts[NUM] = _t(0.0)
        results = {proc._is_estimating(NUM, _t(1.5)) for _ in range(50)}
        self.assertEqual(results, {True})


if __name__ == "__main__":
    unittest.main()
