"""FWnY5RhK RED GATE (`docs/artifacts/2026-08-20-075-position-validity-flag-fix/`
requirement-spec pulled from data-investigation.md, confirmed at `position_processor.py:303`
/ `dp_reckoner.py:206-207`, revision 63ae958):

`PositionProcessor._handle_position` calls `self._reckoner.observe_real_position(num,
dist_pct, clock_time)` UNCONDITIONALLY, before the byte-identical-repeat check six lines
later (`if prev and prev == (rx, ry, dist_pct): continue`). `observe_real_position` writes
`_real_dp`/`_real_ts` unconditionally too (`dp_reckoner.py:206-207`). So a frozen (X, Y)
repeat -- the car's position feed not updating while the car is actually moving -- is
recorded as a FRESH real fix every single time, and `DpReckoner.ms_since_last_known()` reads
~0 for the entire freeze instead of growing to reflect how stale the position actually is.

Confirmed against real archive data in `data-investigation.md` §6.1/§6.2 by executing the
shipped `DpReckoner`/`PositionProcessor` over real captures (not by reading code alone):

  - Spa Q, car 23, 14:08:36: a 2.98s freeze, 12 byte-identical fixes, `msSinceLastKnown` =
    0.0 at EVERY ONE of them, while the car's own CarData speed stayed 223->201 km/h
    throughout (moving).
  - Budapest R, car 12, 13:34:49-13:35:21: a 32.14s freeze, CarData hand-verified fresh and
    varying (spd 249->145->110->100->182->125->127 km/h) the whole time -- a car braking,
    cornering and accelerating through a lap, not stationary.
  - Montreal FP1, car 1, 17:18:13-17:30:05: the worst case in the corpus, a 712.2s (11m59s)
    freeze at racing speed (57-304 km/h), `msSinceLastKnown` reading mean 147ms / max
    1,164ms as shipped against a corrected mean of 357,170ms / max 711,185ms.

`file-impact-map.md` §5 names these three windows as the fixture material for this phase and
gives the fix shape (a `refresh_real_anchor` kwarg on `observe_real_position`, gated by
`position_processor`'s own `self._r_speed` against a `MOTION_REPEAT_SPEED_KPH = 30.0`
threshold) -- but that fix has NOT been implemented yet at the revision this file is written
against. This file is the Red Gate: written from `data-investigation.md` and
`file-impact-map.md` alone, driving `PositionProcessor._handle_position`/`_handle_car_data`
directly (the same white-box technique `test_position_processor_max_dt_stall_discard_ac7.py`
and `test_position_processor_is_estimating_ac6.py` already use), reproducing each window's
freeze DURATION and CarData SPEED synthetically (not by parsing the raw archive files -- the
values above are the oracle, taken verbatim from data-investigation.md's own execution of the
real code over the real captures) on the same degenerate single-segment track fixture the
sibling position_processor tests already use.

Per `.claude/rules/testing.md` ("The Red Gate"): this file must be observed FAILING against
today's unfixed code before any implementation work begins on FWnY5RhK. `VoKe1ezT` (the
`Position.Status` question) is separately closed as MOOT with no code change
(data-investigation.md §5) and has no tests here.

Also covers the regression the NAIVE fix ("never refresh `_real_ts` on any byte-identical
repeat") would have introduced, per data-investigation.md §8.1: 92.4% of byte-identical
repeats are STATIONARY cars (garage, pit box, grid) whose repeated fix is a genuine real fix.
`StationaryRepeatMustNotBeMarkedStale` proves that side is protected -- it must pass BOTH
before and after the fix, and is exactly as important as the freeze-detection tests above,
since a fix that made it fail would just be trading one regression for a bigger one.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

from app.processing.processors.position_processor import PositionProcessor
from app.processing.track_geometry import TrackGeometry

NUM = "1"

# The archive-verified motion speeds each freeze window occurred at
# (data-investigation.md §6.2, §6.3) -- all well above the 30 km/h
# motion-gate threshold file-impact-map.md §0.5/§2.2 specifies.
SPA_SPEED_KPH = 223.0
BUDAPEST_SPEED_KPH = 249.0
MONTREAL_SPEED_KPH = 200.0

# The archive-verified freeze durations (data-investigation.md §6.2).
SPA_FREEZE_S = 2.9795          # Spa Q car 23, 14:08:36 -- 12 fixes, 2,979.5 ms
BUDAPEST_FREEZE_S = 32.14      # Budapest R car 12, 13:34:49 -> 13:35:21.280
MONTREAL_FREEZE_S = 712.2      # Montreal FP1 car 1, 17:18:13 -> 17:30:05, worst in corpus

STATIONARY_SPEED_KPH = 0.0     # a parked/garage/pit-box car -- 92.4% of repeats (§8.1)
NEAR_BOUNDARY_MOVING_KPH = 30.001   # just above MOTION_REPEAT_SPEED_KPH -- must count as moving
AT_BOUNDARY_KPH = 30.0              # exactly the threshold -- strict `>`, must NOT count as moving


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _degenerate_geo(total_dist: float = 100.0) -> TrackGeometry:
    """A minimal single-segment "track" -- enough for `project_local`'s projection to run
    without a real SVG. Same construction the sibling position_processor tests use
    (`test_position_processor_max_dt_stall_discard_ac7.py`,
    `test_position_processor_is_estimating_ac6.py`)."""
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
    proc = PositionProcessor(_mock_bus(), "race")
    proc._geo = _degenerate_geo()
    return proc


def _position_payload(x: float, y: float) -> dict:
    return {"Position": [{"Entries": {NUM: {"X": x, "Y": y}}}]}


def _car_data(speed_kph: float) -> dict:
    return {"Entries": [{"Cars": {NUM: {"Channels": {
        "2": speed_kph,   # speed (km/h)
        "4": 100,         # throttle % -- steady full throttle, no braking, so
        "5": 0,           # _smooth_speed's impossible-blip filter never engages
    }}}}]}


def _freeze(proc: PositionProcessor, x: float, y: float, speed_kph: float,
            first_fix_ts: datetime, repeat_ts: datetime) -> None:
    """Drives the exact sequence that produces a stale-position freeze in the real feed:
    a real position fix, a CarData sample establishing the car's speed, then a
    byte-identical repeated position fix after `repeat_ts - first_fix_ts` seconds. Mirrors
    `data-investigation.md` §6.1's own description of the mechanism at
    `position_processor.py:303-309`."""
    proc._handle_position(_position_payload(x, y), first_fix_ts)
    proc._handle_car_data(_car_data(speed_kph), first_fix_ts + timedelta(milliseconds=50))
    proc._handle_position(_position_payload(x, y), repeat_ts)


class MotionFreezeStalenessBlind_RedGate(unittest.TestCase):
    """The bug itself: a byte-identical position repeat while the car is MOVING (not
    stationary) must leave `ms_since_last_known` reflecting the true elapsed time since the
    last genuinely fresh fix -- not reset to ~0 on every repeated message. Must FAIL against
    today's unfixed `position_processor.py`/`dp_reckoner.py`."""

    def test_spa_style_2_98s_moving_freeze_ms_since_last_known_must_grow_not_reset(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=SPA_FREEZE_S)

        _freeze(proc, x=10.0, y=0.0, speed_kph=SPA_SPEED_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        expected_ms = SPA_FREEZE_S * 1000.0   # ~2,979.5 ms, matching data-investigation.md §6.2

        self.assertGreater(
            actual_ms, expected_ms - 5.0,
            msg=(
                f"FWnY5RhK unfixed: after a {SPA_FREEZE_S}s byte-identical position repeat "
                f"while the car was moving at {SPA_SPEED_KPH} km/h (Spa Q car 23, "
                f"14:08:36, data-investigation.md §6.2), ms_since_last_known reads "
                f"{actual_ms} instead of ~{expected_ms:.1f} -- observe_real_position() is "
                f"still called unconditionally BEFORE the repeat check at "
                f"position_processor.py:303-309, so the repeat is recorded as a fresh real "
                f"fix and _real_ts resets to the repeat's own timestamp every time."
            ),
        )

    def test_budapest_style_32s_moving_freeze_ms_since_last_known_must_grow_not_reset(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=BUDAPEST_FREEZE_S)

        _freeze(proc, x=20.0, y=0.0, speed_kph=BUDAPEST_SPEED_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        expected_ms = BUDAPEST_FREEZE_S * 1000.0   # ~32,140 ms

        self.assertGreater(
            actual_ms, expected_ms - 5.0,
            msg=(
                f"FWnY5RhK unfixed: a {BUDAPEST_FREEZE_S}s freeze (Budapest R car 12, "
                f"13:34:49–13:35:21.280, CarData hand-verified fresh/varying throughout, "
                f"data-investigation.md §6.3) reads ms_since_last_known={actual_ms} "
                f"instead of ~{expected_ms:.1f}."
            ),
        )

    def test_montreal_style_712s_worst_case_moving_freeze_ms_since_last_known_must_grow(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=MONTREAL_FREEZE_S)

        _freeze(proc, x=30.0, y=0.0, speed_kph=MONTREAL_SPEED_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        expected_ms = MONTREAL_FREEZE_S * 1000.0   # ~712,200 ms

        self.assertGreater(
            actual_ms, expected_ms - 5.0,
            msg=(
                f"FWnY5RhK unfixed: the worst case in the corpus (Montreal FP1 car 1, "
                f"17:18:13–17:30:05, 57-304 km/h throughout, data-investigation.md "
                f"§6.2 -- shipped code measured mean 147ms/max 1,164ms against a "
                f"corrected mean 357,170ms/max 711,185ms) reads ms_since_last_known="
                f"{actual_ms} instead of ~{expected_ms:.1f} at the freeze's own end -- "
                f"understated by {expected_ms - actual_ms:.0f} ms."
            ),
        )

    def test_repeated_intermediate_fixes_during_a_freeze_never_show_growing_staleness(self):
        """Not just the endpoints: EVERY repeat within a moving freeze resets _real_ts today
        (data-investigation.md §6.2's Spa table: "msSinceLastKnown = 0.0 at every one" of the
        12 fixes, not just the last), so this must catch a fix that only patches the freeze's
        final reading while leaving every intermediate repeat still resetting to ~0.

        NOTE (backend-specialist, Phase 4): this method originally also carried a hard
        `assertTrue(all(r < 10.0 for r in readings))` asserting the UNFIXED, near-zero
        reading as a live check -- self-contradictory with the assertion below once the fix
        is correct (readings necessarily grow past 10ms as motion-gated staleness accrues),
        so the method could never pass in both the red and the green state by construction.
        That was a Phase-3 test-authoring defect (documenting pre-fix behavior as an
        assertion rather than a comment), not a production-code issue -- removed here, red
        gate is unaffected: the original red run failed on the `assertGreater` below, not on
        the removed line (test-plan.md §3: "AssertionError: 0.0 not greater than 125.0")."""
        proc = _proc()
        first_ts = _t(0.0)
        proc._handle_position(_position_payload(10.0, 0.0), first_ts)
        proc._handle_car_data(_car_data(SPA_SPEED_KPH), first_ts + timedelta(milliseconds=50))

        gap_s = 0.25
        readings = []
        for i in range(1, 13):
            ts = first_ts + timedelta(seconds=gap_s * i)
            proc._handle_position(_position_payload(10.0, 0.0), ts)
            readings.append(proc._reckoner.ms_since_last_known(NUM, ts))

        self.assertGreater(
            max(readings), gap_s * 1000.0 * 0.5,
            msg="expected staleness to have grown to at least half an inter-repeat gap "
                "by the end of the freeze once the motion gate is in place -- if this "
                "already holds against unfixed code, the fixture does not exercise the bug.",
        )


class StationaryRepeatMustNotBeMarkedStale(unittest.TestCase):
    """The regression the NAIVE fix would introduce (data-investigation.md §8.1): 92.4% of
    byte-identical repeats are a car sitting still (garage/pit box/grid), and that repeat IS a
    genuine real fix. These must pass BOTH before and after the fix -- a motion-gated fix must
    keep refreshing the real anchor for a stationary car exactly like today."""

    def test_stationary_car_repeat_ms_since_last_known_stays_near_zero(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=45.0)   # long enough that a false-stale
                                                          # verdict would be obvious

        _freeze(proc, x=40.0, y=0.0, speed_kph=STATIONARY_SPEED_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        self.assertLess(
            actual_ms, 5.0,
            msg=(
                f"A stationary car's byte-identical repeat must keep refreshing the real "
                f"fix anchor -- it is a genuine real fix, not staleness. Got "
                f"ms_since_last_known={actual_ms} after a 45s stationary repeat; the naive "
                f"'never refresh on any repeat' fix (data-investigation.md §8.1, REFUTED) "
                f"would have broken exactly this case for 92.4% of all repeats in the corpus."
            ),
        )

    def test_car_with_no_cardata_yet_first_repeat_defaults_to_stationary(self):
        """`self._r_speed.get(num, 0.0)` -- a car whose first-ever repeat happens before any
        CarData has arrived for it must default to the stationary (real-anchor-refreshing)
        path, not be treated as moving -- confirming no new false-positive class is
        introduced by the motion gate (file-impact-map.md §5)."""
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=45.0)

        proc._handle_position(_position_payload(50.0, 0.0), first_ts)
        self.assertNotIn(NUM, proc._r_speed, "test setup: no CarData must have arrived yet")
        proc._handle_position(_position_payload(50.0, 0.0), repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        self.assertLess(
            actual_ms, 5.0,
            msg=f"a repeat with no CarData seen yet must default to stationary (real anchor "
                f"keeps refreshing), got ms_since_last_known={actual_ms}",
        )


class MotionGateBoundary(unittest.TestCase):
    """The exact 30 km/h boundary (file-impact-map.md §2.2/§6: `MOTION_REPEAT_SPEED_KPH
    = 30.0`, strict `>`, matching the existing strict-`>` convention already used at the AC-14
    gate). These currently pass trivially (today's code refreshes on every repeat regardless
    of speed) -- they exist to lock the boundary in place once the motion gate lands, and are
    listed here (rather than deferred to the green gate) so the red-gate suite is already
    complete against file-impact-map.md §5's enumerated branches."""

    def test_speed_exactly_at_threshold_is_not_treated_as_moving(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=45.0)

        _freeze(proc, x=60.0, y=0.0, speed_kph=AT_BOUNDARY_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        self.assertLess(
            actual_ms, 5.0,
            msg=f"exactly {AT_BOUNDARY_KPH} km/h (the threshold itself) must NOT count as "
                f"moving (strict '>' per file-impact-map.md §2.2), got "
                f"ms_since_last_known={actual_ms} after a 45s repeat",
        )

    def test_speed_just_above_threshold_is_treated_as_moving(self):
        proc = _proc()
        first_ts = _t(0.0)
        repeat_ts = first_ts + timedelta(seconds=45.0)

        _freeze(proc, x=70.0, y=0.0, speed_kph=NEAR_BOUNDARY_MOVING_KPH,
                first_fix_ts=first_ts, repeat_ts=repeat_ts)

        actual_ms = proc._reckoner.ms_since_last_known(NUM, repeat_ts)
        self.assertGreater(
            actual_ms, 44_000.0,
            msg=(
                f"FWnY5RhK unfixed: {NEAR_BOUNDARY_MOVING_KPH} km/h (just above the "
                f"threshold) must be treated as moving once the fix lands -- got "
                f"ms_since_last_known={actual_ms} after a 45s repeat, expected ~45,000. "
                f"This is a red-gate assertion too: today's code has no motion gate at all, "
                f"so it fails identically to the endpoint tests above."
            ),
        )


if __name__ == "__main__":
    unittest.main()
