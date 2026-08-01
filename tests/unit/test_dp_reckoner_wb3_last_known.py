"""WB3 `DpReckoner` extension (`docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/`
implementation-plan.md "WB3", requirement-spec.md §8.1, AC-2, AC-12): direct unit tests for
`_real_dp`/`_real_ts` and the `last_known_dp`/`ms_since_last_known` accessors this work block
adds to `DpReckoner`.

Written in the same style/isolation level as `tests/unit/test_dp_reckoner.py` (WB1) -- these
exercise `DpReckoner` directly, not through either processor -- from a full read of
`dp_reckoner.py` as shipped (post-WB3), not derived by reading `telemetry_processor.py`'s call
sites. The one property these tests are specifically built to pin down, because it is the entire
reason this API extension exists (§8.1): `advance()` must NEVER touch `_real_dp`/`_real_ts` --
only `observe_real_position()` may. If a future edit accidentally makes `advance()` write to
either dict, `last_known_dp`/`ms_since_last_known` collapse to `current_dp()`/the last
integration time and lose the "what did we last KNOW for certain" meaning §8.1 designs them for.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.dp_reckoner import DpReckoner, MIN_CAL_N, EST_THRESHOLD_S

NUM_A = "44"
NUM_B = "1"


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _seeded(num: str = NUM_A, *, c: float = 0.01) -> DpReckoner:
    """A reckoner with calibration already established (bypassing the
    MIN_CAL_N bootstrap) -- same seeding shortcut `test_dp_reckoner.py`'s
    own `advance`-focused tests use, since these tests are about the
    real-fix-anchor accessors, not calibration convergence."""
    r = DpReckoner()
    r._C = c
    r._cal_n = MIN_CAL_N
    return r


class LastKnownDpBeforeAnyRealFix(unittest.TestCase):
    def test_last_known_dp_is_none_before_any_real_fix(self):
        r = DpReckoner()
        self.assertIsNone(r.last_known_dp(NUM_A))

    def test_ms_since_last_known_is_none_before_any_real_fix(self):
        r = DpReckoner()
        self.assertIsNone(r.ms_since_last_known(NUM_A, _t(100.0)))

    def test_last_known_dp_stays_none_after_advance_alone_with_no_real_fix(self):
        """advance() alone (never preceded by observe_real_position) must not
        seed _real_dp/_real_ts -- there is no real fix to anchor to yet, and
        `advance()` on an unseeded car returns dp=None anyway (dp_reckoner.py's
        own is_seeded() precondition), so there is nothing for it to leak from."""
        r = _seeded()
        r.advance(NUM_A, 300.0, _t(5.0))
        self.assertIsNone(r.last_known_dp(NUM_A))
        self.assertIsNone(r.ms_since_last_known(NUM_A, _t(5.0)))


class LastKnownDpAfterARealFix(unittest.TestCase):
    def test_last_known_dp_set_after_a_real_fix(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 17.5, _t(0.0))
        self.assertEqual(r.last_known_dp(NUM_A), 17.5)

    def test_ms_since_last_known_is_zero_immediately_after_a_fresh_real_fix(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 17.5, _t(10.0))
        self.assertEqual(r.ms_since_last_known(NUM_A, _t(10.0)), 0.0)

    def test_ms_since_last_known_grows_with_elapsed_message_time(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(0.25)), 250.0, places=6)
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(1.0)), 1000.0, places=6)
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(60.0)), 60_000.0, places=6)

    def test_ms_since_last_known_is_a_pure_read_callable_any_number_of_times(self):
        """The whole point of the design (§8.1): reading these accessors has
        no ordering dependency and no idempotency hazard -- unlike advance(),
        calling them repeatedly for the same clock_time must never change
        anything or produce a different answer."""
        r = DpReckoner()
        r.observe_real_position(NUM_A, 5.0, _t(0.0))
        first = r.ms_since_last_known(NUM_A, _t(2.0))
        for _ in range(10):
            self.assertEqual(r.ms_since_last_known(NUM_A, _t(2.0)), first)
        self.assertEqual(r.last_known_dp(NUM_A), 5.0)


class AdvanceNeverTouchesRealFixAnchor(unittest.TestCase):
    """The load-bearing property (§8.1): `_real_dp`/`_real_ts` hold steady
    across any number of `advance()` calls between real fixes -- only
    `observe_real_position()` may write them."""

    def test_real_dp_stays_anchored_across_a_single_advance_call(self):
        r = _seeded()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(1.0))    # dead-reckon forward -- must NOT move last_known_dp
        self.assertEqual(r.last_known_dp(NUM_A), 10.0)
        self.assertNotEqual(
            r.current_dp(NUM_A), r.last_known_dp(NUM_A),
            "test setup: advance() must actually have moved current_dp away "
            "from the real-fix anchor, or this test proves nothing",
        )

    def test_real_dp_stays_anchored_across_many_subsequent_advance_calls(self):
        r = _seeded()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        for i in range(1, 21):
            r.advance(NUM_A, 300.0, _t(i))
        self.assertEqual(r.last_known_dp(NUM_A), 10.0, "20 advance() calls must not move the anchor")
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(20.0)), 20_000.0, places=6)
        self.assertNotAlmostEqual(r.current_dp(NUM_A), 10.0, places=3,
                                   msg="test setup: dp must have actually drifted forward")

    def test_real_dp_and_ts_update_only_on_the_next_real_fix(self):
        r = _seeded()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(1.0))
        r.advance(NUM_A, 300.0, _t(2.0))
        self.assertEqual(r.last_known_dp(NUM_A), 10.0)

        r.observe_real_position(NUM_A, 25.0, _t(3.0))    # a new real fix arrives
        self.assertEqual(r.last_known_dp(NUM_A), 25.0, "a new real fix must update the anchor")
        self.assertEqual(r.ms_since_last_known(NUM_A, _t(3.0)), 0.0)

    def test_ms_since_last_known_resets_to_a_small_value_on_the_next_real_fix(self):
        r = _seeded()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(5.0))
        self.assertGreater(r.ms_since_last_known(NUM_A, _t(5.0)), 0.0)

        r.observe_real_position(NUM_A, 12.0, _t(5.1))
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(5.1)), 0.0, places=6)
        self.assertAlmostEqual(r.ms_since_last_known(NUM_A, _t(5.3)), 200.0, places=6)


class MultiCarIsolation(unittest.TestCase):
    def test_real_dp_real_ts_do_not_leak_between_cars(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 10.0, _t(0.0))
        r.observe_real_position(NUM_B, 80.0, _t(0.0))

        r.observe_real_position(NUM_A, 20.0, _t(5.0))   # only car A gets a fresh real fix

        self.assertEqual(r.last_known_dp(NUM_A), 20.0)
        self.assertEqual(r.last_known_dp(NUM_B), 80.0, "car B's anchor must be untouched by car A's fix")
        self.assertEqual(r.ms_since_last_known(NUM_A, _t(5.0)), 0.0)
        self.assertAlmostEqual(r.ms_since_last_known(NUM_B, _t(5.0)), 5000.0, places=6)

    def test_advance_on_one_car_does_not_affect_another_cars_anchor(self):
        r = _seeded()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        r.observe_real_position(NUM_B, 50.0, _t(0.0))
        r.advance(NUM_A, 300.0, _t(3.0))    # only car A moves
        self.assertEqual(r.last_known_dp(NUM_B), 50.0)
        self.assertEqual(r.ms_since_last_known(NUM_B, _t(3.0)), 3000.0)

    def test_unseeded_car_stays_none_independent_of_a_seeded_sibling(self):
        r = DpReckoner()
        r.observe_real_position(NUM_A, 0.0, _t(0.0))
        self.assertIsNone(r.last_known_dp(NUM_B))
        self.assertIsNone(r.ms_since_last_known(NUM_B, _t(1.0)))


class EstThresholdConstant(unittest.TestCase):
    """WB3 moves EST_THRESHOLD_S into this module (from position_processor.py,
    same value/meaning) -- covered here since it now lives in this file."""

    def test_est_threshold_s_value_unchanged_from_wb2(self):
        self.assertEqual(EST_THRESHOLD_S, 1.0)

    def test_position_processor_re_exports_the_same_object(self):
        from app.processing.processors.position_processor import EST_THRESHOLD_S as pp_threshold
        self.assertIs(pp_threshold, EST_THRESHOLD_S, "must be a re-export, not a second literal")

    def test_telemetry_processor_imports_the_same_object(self):
        from app.processing.processors.telemetry_processor import EST_THRESHOLD_S as tp_threshold
        self.assertIs(tp_threshold, EST_THRESHOLD_S)


class DeterminismAC12(unittest.TestCase):
    def test_last_known_dp_and_ms_since_are_deterministic_across_identical_runs(self):
        """AC-12: the new accessors must not introduce any dependence on
        wall-clock time or call order -- same call sequence, same result,
        every time."""
        def run() -> list:
            r = DpReckoner()
            out = []
            out.append(r.observe_real_position(NUM_A, 0.0, _t(0.0)))
            for i in range(1, 30):
                r.advance(NUM_A, 150.0, _t(i))
                out.append(r.last_known_dp(NUM_A))
                out.append(r.ms_since_last_known(NUM_A, _t(i)))
                if i % 7 == 0:
                    r.observe_real_position(NUM_A, (i * 2.1) % 100.0, _t(i))
                    out.append(r.last_known_dp(NUM_A))
            return out

        self.assertEqual(run(), run())

    def test_no_wall_clock_reads_in_the_new_accessors(self):
        """Static (AST) guard, same technique as test_dp_reckoner.py's own
        determinism test: no CALL to a wall-clock function appears anywhere
        in the module, including the WB3-added code."""
        import ast
        import inspect
        import app.processing.dp_reckoner as mod

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


if __name__ == "__main__":
    unittest.main()
