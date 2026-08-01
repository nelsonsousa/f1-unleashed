"""Direct unit tests for `DpReckoner` (app/processing/dp_reckoner.py), WB1
of docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/.

These tests exercise `DpReckoner` in isolation -- NOT through
`PositionProcessor` -- per this task's instruction: the AC-7 stall case is
already covered end-to-end at the `PositionProcessor` level by
`tests/regression/test_position_processor_max_dt_stall_discard_ac7.py` (the
red gate for the `MAX_DT_S` bug); this file instead proves the module's own
contract directly, including properties the red-gate test doesn't reach
(calibration convergence/pre-calibration state, idempotency, multi-car
isolation, determinism, and the `SAFE_MAX_DT_S` pathological-input guard).

Written from `requirement-spec.md` (AC-7, AC-12), `implementation-plan.md`,
and a full read of `dp_reckoner.py` itself -- not derived by reading
`position_processor.py`'s call sites, so this is testing the module's own
documented behavior/contract, not incidentally re-deriving what one caller
happens to do with it.
"""
import logging
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.dp_reckoner import (
    DpReckoner,
    AdvanceResult,
    MIN_CAL_N,
    CAL_DDP_MIN,
    CAL_DDP_MAX,
    CAL_CARDATA_DT_MAX_S,
    CAL_LOG_AT_N,
    SAFE_MAX_DT_S,
)

NUM_A = "44"
NUM_B = "1"


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _calibrate(r: DpReckoner, num: str, *, n: int, speed: float = 100.0,
               dt: float = 1.0, ddp: float = 1.0, start: float = 0.0) -> float:
    """Drive `n` real-position + CarData ticks for `num` so the shared
    calibration factor accumulates exactly `n` valid (dp, sv) intervals.
    A seed pair (`observe_real_position` + `advance` at the same
    timestamp) anchors both accumulators with zero contribution first --
    the real code's first-ever call to each never accumulates anything
    (nothing to diff/dt against yet) -- so every one of the `n` loop
    iterations that follows contributes exactly one matched (ddp, sv)
    pair to `cal_dp_sum`/`cal_sv_sum`/`cal_n`. Returns the final real dp
    reached.
    """
    ts = start
    dp = 0.0
    r.observe_real_position(num, dp, _t(ts))     # seed real-position anchor, no accumulation yet
    r.advance(num, speed, _t(ts))                # seed CarData-ts anchor, no accumulation yet (dt==0)
    for _ in range(n):
        ts += dt
        r.advance(num, speed, _t(ts))            # accumulates cal_sv_sum (continuity from seed/prior tick)
        dp += ddp
        r.observe_real_position(num, dp, _t(ts))  # accumulates cal_dp_sum/cal_n
    return dp


class DpReckonerCalibrationTests(unittest.TestCase):
    def test_uncalibrated_before_min_cal_n_samples(self):
        r = DpReckoner()
        self.assertIsNone(r.C)
        self.assertFalse(r.calibrated)
        _calibrate(r, NUM_A, n=MIN_CAL_N - 1, speed=100.0, dt=1.0, ddp=1.0)
        self.assertIsNotNone(r.C, "C should already be estimated once cal_sv_sum > 0")
        self.assertFalse(r.calibrated, "calibrated requires cal_n >= MIN_CAL_N")

    def test_calibrated_exactly_at_min_cal_n_samples(self):
        r = DpReckoner()
        _calibrate(r, NUM_A, n=MIN_CAL_N, speed=100.0, dt=1.0, ddp=1.0)
        self.assertTrue(r.calibrated)

    def test_calibration_converges_to_true_conversion_factor(self):
        """Real dp advances by `ddp = C_true * speed * dt` each tick;
        the learned `C` must converge to `C_true`."""
        c_true = 0.02
        speed = 150.0
        dt = 1.0
        ddp = c_true * speed * dt
        r = DpReckoner()
        _calibrate(r, NUM_A, n=50, speed=speed, dt=dt, ddp=ddp)
        self.assertTrue(r.calibrated)
        self.assertAlmostEqual(r.C, c_true, places=6)

    def test_calibration_ignores_ddp_at_or_below_min_floor(self):
        """A stale-repeat real fix (ddp == 0, at/below CAL_DDP_MIN) must not
        count toward cal_n -- guards against a duplicate fix inflating the
        sample count with a zero-distance interval."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))
        r.observe_real_position(NUM_A, 10.0, _t(1.0))    # ddp == 0 -- repeat fix
        self.assertEqual(r._cal_n, 0)
        self.assertIsNone(r.C)

    def test_calibration_ignores_ddp_at_or_above_max_ceiling(self):
        """A pit-lane jump / lap wrap producing ddp >= CAL_DDP_MAX must not
        count toward calibration -- it isn't a sane forward advance."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))
        r.observe_real_position(NUM_A, CAL_DDP_MAX + 5.0, _t(1.0))   # huge jump
        self.assertEqual(r._cal_n, 0)
        self.assertIsNone(r.C)

    def test_calibration_boundary_ddp_values_are_exclusive(self):
        """CAL_DDP_MIN and CAL_DDP_MAX themselves are excluded (strict `<`
        on both sides) -- exactly-at-the-floor/ceiling does not count."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))
        r.observe_real_position(NUM_A, CAL_DDP_MIN, _t(1.0))   # ddp == floor exactly
        self.assertEqual(r._cal_n, 0, "ddp == CAL_DDP_MIN must not count (strict inequality)")

        r2 = DpReckoner()
        r2.observe_real_position(NUM_A, 0.0, _t(0.0))
        r2.advance(NUM_A, 100.0, _t(1.0))
        r2.observe_real_position(NUM_A, CAL_DDP_MAX, _t(1.0))   # ddp == ceiling exactly
        self.assertEqual(r2._cal_n, 0, "ddp == CAL_DDP_MAX must not count (strict inequality)")

    def test_cal_n_can_advance_without_c_being_set_when_sv_sum_never_positive(self):
        """cal_n increments on every sane ddp regardless of cal_sv_sum, but
        `C` is only computed while cal_sv_sum > 0 -- so it's possible to
        reach cal_n >= MIN_CAL_N with C still None (never went through
        `advance()`, e.g. only Position fixes ever arrived)."""
        r = DpReckoner()
        dp = 0.0
        r.observe_real_position(NUM_A, dp, _t(0.0))
        for i in range(MIN_CAL_N):
            dp += 1.0
            r.observe_real_position(NUM_A, dp, _t(i + 1.0))   # no advance() calls -> cal_sv_sum stays 0
        self.assertGreaterEqual(r._cal_n, MIN_CAL_N)
        self.assertIsNone(r.C)
        self.assertFalse(r.calibrated, "calibrated is False whenever C is None, even if cal_n is high")

    def test_one_shot_calibration_log_fires_exactly_at_cal_log_at_n(self):
        r = DpReckoner()
        with self.assertLogs("app.processing.dp_reckoner", level="INFO") as cm:
            _calibrate(r, NUM_A, n=CAL_LOG_AT_N, speed=10.0, dt=1.0, ddp=1.0)
        self.assertEqual(len(cm.output), 1, "the diagnostic log must fire exactly once")
        self.assertIn("calibrated C=", cm.output[0])

        # One tick further: no additional log record.
        with self.assertRaises(AssertionError):
            with self.assertLogs("app.processing.dp_reckoner", level="INFO"):
                r.advance(NUM_A, 10.0, _t(10_000.0))
                r.observe_real_position(NUM_A, 999.0 % 100.0, _t(10_000.0))

    def test_advance_rejects_dtc_at_ceiling_exactly(self):
        """The CarData continuity accumulator (cal_sv_sum) only accepts
        `0 < dtc < CAL_CARDATA_DT_MAX_S` (strict) -- a gap exactly AT the
        ceiling must not feed it. Isolated in its own reckoner so a
        rejected call's own timestamp update (unconditional, same pattern
        as `advance()`'s anchor) doesn't become the baseline for a
        follow-up assertion."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))                     # first CarData tick: no prior, no accumulation
        self.assertEqual(r._cal_sv_sum, 0.0)

        r.advance(NUM_A, 100.0, _t(1.0 + CAL_CARDATA_DT_MAX_S))  # dtc == ceiling exactly -> rejected
        self.assertEqual(r._cal_sv_sum, 0.0, "dtc == CAL_CARDATA_DT_MAX_S must be rejected (strict <)")

    def test_advance_rejects_dtc_above_ceiling(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))                     # first CarData tick: no prior, no accumulation
        r.advance(NUM_A, 100.0, _t(1.0 + CAL_CARDATA_DT_MAX_S + 0.5))  # a too-large gap
        self.assertEqual(r._cal_sv_sum, 0.0)

    def test_advance_sv_sum_rejects_non_positive_dtc(self):
        """A repeated or out-of-order CarData timestamp (dtc <= 0) must not
        feed cal_sv_sum either."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))
        r.advance(NUM_A, 100.0, _t(1.0))       # dtc == 0, repeat timestamp
        self.assertEqual(r._cal_sv_sum, 0.0)


class DpReckonerAdvanceTests(unittest.TestCase):
    def test_advance_before_any_real_fix_returns_none_and_is_a_no_op(self):
        r = DpReckoner()
        self.assertFalse(r.is_seeded(NUM_A))
        result = r.advance(NUM_A, 200.0, _t(5.0))
        self.assertEqual(result, AdvanceResult(dp=None, ddp=None, dt=0.0, calibrated=False))
        self.assertIsNone(r.current_dp(NUM_A))

    def test_advance_returns_none_while_uncalibrated_even_when_seeded(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        self.assertTrue(r.is_seeded(NUM_A))
        result = r.advance(NUM_A, 200.0, _t(1.0))
        self.assertIsNone(result.dp)
        self.assertFalse(result.calibrated)
        # The anchor timestamp still advances even though nothing was integrated.
        self.assertEqual(r._ts[NUM_A], _t(1.0))

    def test_advance_integrates_normally_once_calibrated(self):
        r = DpReckoner()
        c_true = 0.01
        _calibrate(r, NUM_A, n=MIN_CAL_N, speed=100.0, dt=1.0, ddp=c_true * 100.0 * 1.0)
        anchor_dp = r.current_dp(NUM_A)
        anchor_ts = r._ts[NUM_A]

        speed = 250.0
        dt = 2.0
        result = r.advance(NUM_A, speed, anchor_ts + timedelta(seconds=dt))

        expected_ddp = r.C * speed * dt
        self.assertAlmostEqual(result.ddp, expected_ddp, places=9)
        self.assertAlmostEqual(result.dt, dt, places=9)
        self.assertTrue(result.calibrated)
        self.assertAlmostEqual(result.dp, (anchor_dp + expected_ddp) % 100.0, places=9)
        self.assertAlmostEqual(r.current_dp(NUM_A), result.dp, places=9)

    def test_advance_wraps_dp_past_100(self):
        r = DpReckoner()
        r._C = 1.0             # directly seed a known, simple calibration factor
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 95.0, _t(0.0))
        result = r.advance(NUM_A, 36.0, _t(1.0))     # ddp = 1.0*36*1 = 36 -> 95+36=131 -> wraps to 31
        self.assertAlmostEqual(result.dp, 31.0, places=9)

    def test_advance_ac7_stall_longer_than_old_max_dt_s_integrates_full_distance(self):
        """AC-7 at the DpReckoner level directly: a 5.0s CarData stall (more
        than double the retired MAX_DT_S=2.0s) must integrate the FULL
        elapsed distance, not a clamped/truncated portion."""
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))

        speed = 300.0
        stall_s = 5.0
        self.assertGreater(stall_s, 2.0, "test setup: must exceed the old MAX_DT_S=2.0s")
        result = r.advance(NUM_A, speed, _t(stall_s))

        expected_dp = 0.01 * speed * stall_s     # 15.0 -- NOT the old-clamp value of 6.0
        self.assertAlmostEqual(result.dp, expected_dp, places=9)
        self.assertAlmostEqual(result.ddp, expected_dp, places=9)
        self.assertAlmostEqual(result.dt, stall_s, places=9,
                                msg="dt must be the full elapsed stall, not clamped to 2.0s")

    def test_safe_max_dt_s_does_not_clamp_the_ac7_five_second_case(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        result = r.advance(NUM_A, 300.0, _t(5.0))
        self.assertEqual(result.dt, 5.0)
        self.assertLess(5.0, SAFE_MAX_DT_S)

    def test_safe_max_dt_s_clamps_pathological_gap(self):
        """A genuinely pathological (corrupted/out-of-order-recovered)
        multi-hour gap IS clamped by SAFE_MAX_DT_S -- this is the guard the
        module docstring says is NOT the AC-7 clamp reincarnated, just a
        sanity ceiling three orders of magnitude above any real stall."""
        r = DpReckoner()
        r._C = 0.001
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        pathological_gap = SAFE_MAX_DT_S * 10.0
        result = r.advance(NUM_A, 100.0, _t(pathological_gap))
        self.assertEqual(result.dt, SAFE_MAX_DT_S)
        self.assertAlmostEqual(result.ddp, 0.001 * 100.0 * SAFE_MAX_DT_S, places=6)

    def test_advance_zero_dt_is_a_safe_no_op(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 5.0, _t(0.0))
        result = r.advance(NUM_A, 300.0, _t(0.0))    # same clock_time as the anchor
        self.assertIsNone(result.dp)
        self.assertEqual(result.dt, 0.0)
        self.assertEqual(r.current_dp(NUM_A), 5.0, "dp must not change on a dt==0 tick")

    def test_advance_negative_out_of_order_dt_does_not_integrate_backward(self):
        """A clock_time earlier than the car's last anchor (out-of-order
        delivery) must not integrate a negative distance -- dt is reported
        as 0.0, not negative, and no dp change occurs."""
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 5.0, _t(10.0))
        result = r.advance(NUM_A, 300.0, _t(9.0))    # 1s BEFORE the anchor
        self.assertIsNone(result.dp)
        self.assertEqual(result.dt, 0.0, "dt must be floored at 0.0, never negative")
        self.assertEqual(r.current_dp(NUM_A), 5.0)
        # The anchor timestamp still moves to the (earlier) clock_time -- unconditional per
        # the idempotency/ordering-hazard design (module docstring).
        self.assertEqual(r._ts[NUM_A], _t(9.0))

    def test_advance_zero_speed_integrates_zero_distance(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 42.0, _t(0.0))
        result = r.advance(NUM_A, 0.0, _t(3.0))
        self.assertEqual(result.ddp, 0.0)
        self.assertEqual(result.dp, 42.0)
        self.assertEqual(result.dt, 3.0)
        self.assertTrue(result.calibrated)


class DpReckonerIdempotencyTests(unittest.TestCase):
    def test_repeat_advance_call_same_clock_time_is_a_safe_no_op(self):
        """The registration-order hazard the module docstring describes:
        two callers (or one caller invoked twice) for the same car at the
        same `clock_time` must never double-integrate. First call
        integrates normally; the immediate repeat at the identical
        `clock_time` must see dt == 0 and leave dp unchanged."""
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))

        first = r.advance(NUM_A, 300.0, _t(2.0))
        self.assertIsNotNone(first.dp)
        dp_after_first = r.current_dp(NUM_A)

        second = r.advance(NUM_A, 300.0, _t(2.0))    # identical clock_time -- the hazard case
        self.assertIsNone(second.dp, "a repeat call at the same clock_time must not re-integrate")
        self.assertEqual(second.dt, 0.0)
        self.assertEqual(
            r.current_dp(NUM_A), dp_after_first,
            "dp must be unchanged by the repeat call -- proves no double-integration occurred",
        )

    def test_repeat_advance_call_is_safe_regardless_of_call_count(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(2.0))
        dp_after_first = r.current_dp(NUM_A)

        for _ in range(5):
            result = r.advance(NUM_A, 300.0, _t(2.0))
            self.assertIsNone(result.dp)
            self.assertEqual(r.current_dp(NUM_A), dp_after_first)


class DpReckonerObserveRealPositionTests(unittest.TestCase):
    def test_observe_real_position_seeds_and_anchors_car(self):
        r = DpReckoner()
        self.assertFalse(r.is_seeded(NUM_A))
        r.observe_real_position(NUM_A, 17.5, _t(0.0))
        self.assertTrue(r.is_seeded(NUM_A))
        self.assertEqual(r.current_dp(NUM_A), 17.5)
        self.assertEqual(r._ts[NUM_A], _t(0.0))

    def test_observe_real_position_resets_accumulated_drift(self):
        """A real fix discards whatever the reckoner had drifted to and
        anchors to the real, measured dp instead."""
        r = DpReckoner()
        r._C = 0.5   # a deliberately large factor so drift is obvious
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.advance(NUM_A, 100.0, _t(1.0))     # drifts dp away from 0
        drifted_dp = r.current_dp(NUM_A)
        self.assertNotEqual(drifted_dp, 8.0)

        r.observe_real_position(NUM_A, 8.0, _t(1.5))   # a real fix arrives, disagreeing with drift
        self.assertEqual(r.current_dp(NUM_A), 8.0, "a real fix must override accumulated drift")
        self.assertEqual(r._ts[NUM_A], _t(1.5))

    def test_set_dp_overrides_and_subsequent_advance_integrates_from_it(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(1.0))    # dp now = 10 + 0.01*300*1 = 13.0

        r.set_dp(NUM_A, 50.0)               # caller-applied correction (e.g. SC clamp / anchor snap)
        self.assertEqual(r.current_dp(NUM_A), 50.0)

        result = r.advance(NUM_A, 300.0, _t(2.0))   # next tick must integrate FROM the corrected value
        self.assertAlmostEqual(result.dp, 50.0 + 0.01 * 300.0 * 1.0, places=9)

    def test_set_dp_wraps_modulo_100(self):
        r = DpReckoner()
        r.set_dp(NUM_A, 150.0)
        self.assertEqual(r.current_dp(NUM_A), 50.0)
        r.set_dp(NUM_A, -10.0)
        self.assertEqual(r.current_dp(NUM_A), 90.0)


class DpReckonerMultiCarIsolationTests(unittest.TestCase):
    def test_per_car_anchor_state_does_not_leak_between_cars(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.observe_real_position(NUM_B, 80.0, _t(0.0))

        r.advance(NUM_A, 300.0, _t(1.0))     # only car A moves
        self.assertNotEqual(r.current_dp(NUM_A), 0.0)
        self.assertEqual(r.current_dp(NUM_B), 80.0, "car B's dp must be untouched by car A's advance()")

    def test_advance_on_unseeded_car_does_not_affect_a_seeded_car(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 0.0, _t(0.0))

        result_b = r.advance(NUM_B, 500.0, _t(1.0))   # car B never seeded
        self.assertIsNone(result_b.dp)
        self.assertFalse(r.is_seeded(NUM_B))

        result_a = r.advance(NUM_A, 300.0, _t(1.0))
        self.assertIsNotNone(result_a.dp)

    def test_dp_snapshot_reflects_only_seeded_cars_independently(self):
        r = DpReckoner()
        r._C = 0.01
        r._cal_n = MIN_CAL_N
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.observe_real_position(NUM_B, 20.0, _t(0.0))
        snap = r.dp_snapshot()
        self.assertEqual(snap, {NUM_A: 10.0, NUM_B: 20.0})

        # Mutating the snapshot must not affect the reckoner's own state.
        snap[NUM_A] = 999.0
        self.assertEqual(r.current_dp(NUM_A), 10.0)

    def test_calibration_factor_is_shared_globally_across_cars_by_design(self):
        """Unlike per-car dp/ts anchors, the calibration factor `C` is a
        single global scale learned from EVERY car's motion (module
        docstring: "one physical scale for the whole session") -- verify a
        second car's real-position observations contribute to the SAME `C`
        rather than each car deriving its own."""
        r = DpReckoner()
        c_true = 0.02
        _calibrate(r, NUM_A, n=MIN_CAL_N // 2, speed=100.0, dt=1.0, ddp=c_true * 100.0)
        cal_n_after_a = r._cal_n
        self.assertLess(cal_n_after_a, MIN_CAL_N, "test setup: car A alone must not reach MIN_CAL_N")

        _calibrate(r, NUM_B, n=MIN_CAL_N, speed=100.0, dt=1.0, ddp=c_true * 100.0)
        self.assertGreater(r._cal_n, cal_n_after_a, "car B's observations must add to the SAME cal_n")
        self.assertTrue(r.calibrated)
        self.assertAlmostEqual(r.C, c_true, places=6)


class DpReckonerDeterminismTests(unittest.TestCase):
    def test_identical_call_sequence_produces_identical_output_every_run(self):
        """AC-12: no dependence on wall-clock time -- only on the
        `clock_time` values passed in. Running the exact same sequence of
        calls twice, on two independent DpReckoner instances, must yield
        byte-identical results every time."""
        def run() -> list:
            r = DpReckoner()
            out = []
            out.append(r.observe_real_position(NUM_A, 0.0, _t(0.0)))
            out.append(r.observe_real_position(NUM_B, 50.0, _t(0.0)))
            for i in range(1, 40):
                out.append(r.advance(NUM_A, 120.0 + i, _t(i)))
                out.append(r.advance(NUM_B, 200.0, _t(i)))
                if i % 5 == 0:
                    out.append(r.observe_real_position(NUM_A, (i * 1.3) % 100.0, _t(i)))
            out.append(r.dp_snapshot())
            return out

        run_1 = run()
        run_2 = run()
        self.assertEqual(run_1, run_2)

    def test_output_depends_only_on_message_timestamps_not_call_wall_clock(self):
        """Inserting an artificial real-world delay between calls (simulated
        here -- DpReckoner has no `time.sleep`/`time.time()` dependency to
        actually wait on) must not change the result, since only the
        `clock_time` argument is read, never the system clock."""
        r1 = DpReckoner()
        r1._C = 0.01
        r1._cal_n = MIN_CAL_N
        r1.observe_real_position(NUM_A, 0.0, _t(0.0))
        result_1 = r1.advance(NUM_A, 300.0, _t(5.0))

        r2 = DpReckoner()
        r2._C = 0.01
        r2._cal_n = MIN_CAL_N
        r2.observe_real_position(NUM_A, 0.0, _t(0.0))
        # same clock_time argument, different (irrelevant) real Python call gap
        for _ in range(1000):
            pass
        result_2 = r2.advance(NUM_A, 300.0, _t(5.0))

        self.assertEqual(result_1, result_2)

    def test_module_has_no_wall_clock_dependency(self):
        """Static guard: the module never reads `datetime.now`/`utcnow`/
        `time.time` -- confirms AC-12 by construction, not just by sampled
        behavior."""
        import inspect
        import app.processing.dp_reckoner as mod
        src = inspect.getsource(mod)
        self.assertNotIn(".now(", src)
        self.assertNotIn("utcnow(", src)
        self.assertNotIn("time.time(", src)


if __name__ == "__main__":
    unittest.main()
