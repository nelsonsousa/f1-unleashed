"""WB3 `TelemetryProcessor` integration (`docs/artifacts/2026-08-01-040-merged-position-
telemetry-processor/` implementation-plan.md "WB3"): formal test suite for AC-2, AC-4, and the
`sample[7]` repurposing, derived from `requirement-spec.md` §2.2/§2.4/§8.1/AC-2/AC-3/AC-4 and a
full read of the shipped diff (`telemetry_processor.py`'s `_handle_car_data`/`_emit_lap`,
`dp_reckoner.py`) -- not from the implementer's own manual-sanity-script summary
(implementation-plan.md WB3 §4 step 6), which this suite exists to make durable and
independently re-verifiable.

Covers:
  - The last-entry-vs-other-entries split (§8.1's registration-order resolution): a non-last
    entry's pairing miss is filled by a direct `advance()` call; the batch's LAST entry's
    pairing miss is filled by `current_dp()` -- reading the value `PositionProcessor` is
    assumed to have already advanced for that exact tick (registered first, `preprocessor.py`).
  - `lastKnownDp`/`msSinceLastKnown` present and correct on EVERY `liveTelemetry` sample --
    both a pairing HIT and a pairing MISS, per AC-4's "unconditionally" wording, which
    `implementation-plan.md`'s WB3 section states is unconditional but flags (§5 in
    `requirement-spec.md`) as worth confirming explicitly.
  - A pairing HIT is never overridden by the reckoner -- the implementer's stated design
    decision (implementation-plan.md WB3 §2, "A pairing HIT still wins").
  - `sample[7]`'s repurposing to `msSinceLastKnown` and that it has exactly one reader in the
    module (the AC-14 DTW gate) -- verified by direct AST scan of the shipped source, not
    trusted from the implementer's grep claim.
  - AC-12 determinism: no wall-clock dependence introduced by any of this.

Test-suite AC-14 coverage (the DTW lap-commit gate itself, majority-stale/majority-fresh) lives
in its own file, `tests/regression/test_telemetry_ac14_dtw_staleness_gate.py`, since it exercises
a materially different code path (`_emit_lap`, not `_handle_car_data`).
"""
import ast
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app.processing.processors.telemetry_processor as telemetry_processor_mod
from app.processing.dp_reckoner import DpReckoner, MIN_CAL_N
from app.processing.processors.telemetry_processor import TelemetryProcessor

CAR_A = "44"    # used as the "non-last entry" car in split tests
CAR_B = "1"     # used as the "last entry" car in split tests
C = 0.01        # dp% per (kph*s) -- same stand-in factor sibling WB1/WB2 tests use


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _mock_bus() -> mock.Mock:
    bus = mock.Mock()
    bus.on = mock.Mock()
    emitted: list[tuple[str, object, datetime]] = []
    bus.emit = mock.Mock(side_effect=lambda topic, data, ts: emitted.append((topic, data, ts)))
    bus.emitted_log = emitted
    return bus


def _proc(bus: mock.Mock, reckoner: DpReckoner = None, session_type: str = "practice") -> TelemetryProcessor:
    proc = TelemetryProcessor(bus, session_type, reckoner=reckoner)
    return proc


def _seeded_reckoner(*cars: str, start_ts: datetime = _t(0.0), start_dp: float = 0.0) -> DpReckoner:
    """A DpReckoner already calibrated and anchored to a real fix (at
    `start_ts`) for every car named -- bypasses the MIN_CAL_N bootstrap, same
    shortcut `test_dp_reckoner.py`/`test_position_processor_is_estimating_ac6.py`
    use."""
    r = DpReckoner()
    r._C = C
    r._cal_n = MIN_CAL_N
    for num in cars:
        r.observe_real_position(num, start_dp, start_ts)
    return r


def _live_calls(bus: mock.Mock, num: str) -> list:
    topic = f"liveTelemetry:{num}"
    return [c for c in bus.emitted_log if c[0] == topic]


def _entry(num: str, speed: float, thr: int = 100, brk: int = 0) -> dict:
    return {"Cars": {num: {"Channels": {"2": speed, "4": thr, "5": brk}}}}


class LastEntryVsOtherEntriesSplitAC2(unittest.TestCase):
    """§8.1's registration-order resolution: within ONE `_handle_car_data`
    call carrying multiple `Entries`, only the LAST entry may safely read
    `current_dp()` (because `PositionProcessor` is assumed to have already
    `advance()`d the shared reckoner for that exact tick); every OTHER entry
    is this processor's own sole/first call and must dead-reckon directly
    via `advance()`."""

    def test_non_last_entry_pairing_miss_is_filled_via_direct_advance(self):
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0))
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        # CAR_A is the batch's NON-last entry (index 0 of 2). Nothing has
        # touched the reckoner for CAR_A at t=2.0s yet -- this handler must
        # be the sole/first caller, via a direct advance().
        data = {"Entries": [_entry(CAR_A, speed=300.0), _entry("99", speed=250.0)]}
        proc._handle_car_data(data, _t(2.0))

        drv_a = proc._drv(CAR_A)
        self.assertEqual(len(drv_a.samples), 1)
        self.assertIsNotNone(drv_a.samples[0][0], "a non-last entry's pairing miss must be dead-reckoned, not None")
        expected_dp = 0.0 + C * 300.0 * 2.0
        self.assertAlmostEqual(drv_a.samples[0][0], expected_dp, places=6)

    def test_last_entry_pairing_miss_reads_current_dp_not_a_second_advance(self):
        """The concrete, code-grounded regression case implementation-plan.md
        §5 names explicitly: if the last-entry carve-out is ever dropped or
        miscoded back to a second `advance()` call, this scenario would
        silently produce `dp=None` on every single message. Simulating
        `PositionProcessor` having ALREADY advanced CAR_B for this exact
        tick (registered first) by calling `advance()` on the SAME shared
        reckoner instance before invoking the telemetry handler."""
        reckoner = _seeded_reckoner(CAR_B, start_ts=_t(0.0))
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_B).activated = True

        # Simulate PositionProcessor's own _handle_car_data having already
        # advanced the shared reckoner for CAR_B at this exact clock_time --
        # registration order (preprocessor.py) guarantees this happens
        # BEFORE TelemetryProcessor's own handler runs for the same tick.
        already_advanced = reckoner.advance(CAR_B, 250.0, _t(3.0))
        self.assertIsNotNone(already_advanced.dp, "test setup: the simulated PositionProcessor advance must succeed")

        # CAR_B is the batch's LAST entry (index 0 of 1, i.e. the only entry
        # -- last_idx == 0). A SECOND advance() call at the identical
        # clock_time would hit the idempotency guard and return dp=None; the
        # correct behavior is current_dp(), a pure read of the value the
        # simulated PositionProcessor call already computed.
        data = {"Entries": [_entry(CAR_B, speed=250.0)]}
        proc._handle_car_data(data, _t(3.0))

        drv_b = proc._drv(CAR_B)
        self.assertEqual(len(drv_b.samples), 1)
        self.assertIsNotNone(
            drv_b.samples[0][0],
            "the batch's last entry must read current_dp(), not None from a duplicate advance() call",
        )
        self.assertAlmostEqual(
            drv_b.samples[0][0], already_advanced.dp, places=9,
            msg="the last entry's dp must equal EXACTLY what PositionProcessor's own advance() "
                "already computed for this tick, read via current_dp() -- not re-derived",
        )

    def test_pairing_miss_with_non_numeric_speed_channel_stays_none_not_reckoned(self):
        """Branch coverage: the reckoning fill only fires when the CarData
        entry's speed channel is present and numeric
        (`isinstance(speed, (int, float))`) -- a missing/non-numeric speed
        channel on a pairing miss must leave `dp=None`, exactly as before
        WB3 (there is nothing to integrate from), not raise or coerce."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0))
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        # No "2" (speed) channel at all -> ch.get("2") is None.
        data = {"Entries": [{"Cars": {CAR_A: {"Channels": {"4": 100, "5": 0}}}}]}
        proc._handle_car_data(data, _t(2.0))

        drv = proc._drv(CAR_A)
        self.assertEqual(len(drv.samples), 1)
        self.assertIsNone(drv.samples[0][0], "no numeric speed channel -> dp must stay None on a pairing miss")

    def test_split_exercised_together_two_different_cars_one_batch(self):
        """Both halves of the split in a single `_handle_car_data` call, with
        two DIFFERENT cars so their per-car reckoner state cannot interfere
        with each other: CAR_A (index 0, non-last) dead-reckons directly;
        CAR_B (index 1, last) reads current_dp() off a pre-advanced value."""
        reckoner = _seeded_reckoner(CAR_A, CAR_B, start_ts=_t(0.0))
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True
        proc._drv(CAR_B).activated = True

        # PositionProcessor already advanced CAR_B (the last entry's car)
        # for this exact tick -- CAR_A is untouched.
        pre_b = reckoner.advance(CAR_B, 200.0, _t(1.0))
        self.assertIsNotNone(pre_b.dp)

        data = {"Entries": [_entry(CAR_A, speed=300.0), _entry(CAR_B, speed=200.0)]}
        proc._handle_car_data(data, _t(1.0))

        dp_a = proc._drv(CAR_A).samples[0][0]
        dp_b = proc._drv(CAR_B).samples[0][0]
        self.assertIsNotNone(dp_a)
        self.assertIsNotNone(dp_b)
        self.assertAlmostEqual(dp_a, 0.0 + C * 300.0 * 1.0, places=6,
                                msg="non-last entry (CAR_A) must be a fresh direct advance()")
        self.assertAlmostEqual(dp_b, pre_b.dp, places=9,
                                msg="last entry (CAR_B) must equal the pre-advanced current_dp(), unchanged")


class LastKnownDpAndMsSinceLastKnownOnEverySample(unittest.TestCase):
    """AC-4: `lastKnownDp`/`msSinceLastKnown` are present, and correct, on
    EVERY `liveTelemetry` emit -- a pairing HIT included, not only a miss."""

    def test_present_and_correct_on_a_pairing_miss(self):
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=17.5)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(2.5))

        calls = _live_calls(bus, CAR_A)
        self.assertEqual(len(calls), 1)
        live = calls[0][1]
        self.assertIn("lastKnownDp", live)
        self.assertIn("msSinceLastKnown", live)
        self.assertEqual(live["lastKnownDp"], 17.5, "must be the dp at the last REAL fix, unmoved by dead-reckoning")
        self.assertAlmostEqual(live["msSinceLastKnown"], 2500.0, places=3)

    def test_present_and_correct_on_a_pairing_hit(self):
        """The implementer's claim under test: even a sample whose dp came
        from a real matched position still carries both fields, sourced
        from the SAME shared reckoner's real-fix anchor -- not left absent
        or defaulted just because pairing succeeded."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=40.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        # A real, unconsumed position for CAR_A -- this must WIN the dp
        # source (see PairingHitWinsOverReckoner below), but the two new
        # fields must still be attached.
        proc._handle_position({CAR_A: [0, 0, 63.2]}, _t(4.0))
        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(4.2))

        calls = _live_calls(bus, CAR_A)
        self.assertEqual(len(calls), 1)
        live = calls[0][1]
        self.assertIn("lastKnownDp", live)
        self.assertIn("msSinceLastKnown", live)
        self.assertEqual(live["dp"], 63.2, "test setup: this must actually be a pairing HIT")
        self.assertEqual(live["lastKnownDp"], 40.0, "still sourced from the reckoner's real-fix anchor")
        self.assertAlmostEqual(live["msSinceLastKnown"], 4200.0, places=3)

    def test_both_fields_none_when_reckoner_never_seeded(self):
        """A default, never-externally-seeded reckoner (e.g. the isolated
        per-test-construction default every existing pairing test uses) must
        report both fields as None -- not crash, not fabricate a value --
        for the residual pre-calibration/never-seeded window §5 names."""
        bus = _mock_bus()
        proc = _proc(bus)   # no reckoner passed -> private, unseeded DpReckoner
        proc._drv(CAR_A).activated = True

        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(0.0))

        calls = _live_calls(bus, CAR_A)
        live = calls[0][1]
        self.assertIsNone(live["lastKnownDp"])
        self.assertIsNone(live["msSinceLastKnown"])
        self.assertIsNone(live["dp"], "unseeded reckoner + no pairing -> dp stays None too, unchanged from pre-WB3")


class PairingHitWinsOverReckoner(unittest.TestCase):
    """The implementer's stated design decision (implementation-plan.md WB3
    §2): a real, unconsumed matched position ALWAYS wins as the dp source --
    the reckoner is never consulted at all when a match exists, even if the
    reckoner would compute a materially different value."""

    def test_matched_position_dp_is_used_verbatim_not_the_reckoners_value(self):
        # Seed the reckoner so its own reckoned value would clearly differ
        # from the matched position's dp, to prove the reckoner's value is
        # not blended in or silently overriding the match.
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        proc._handle_position({CAR_A: [0, 0, 12.34]}, _t(1.0))
        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=500.0)]}, _t(1.1))

        drv = proc._drv(CAR_A)
        self.assertEqual(drv.samples[0][0], 12.34, "the matched position's own dp must be used verbatim")
        # Sanity: had this gone through the reckoner instead, the value
        # would NOT be 12.34 (500 kph over 1.1s at C=0.01 integrates far past it).
        reckoner_only_estimate = 0.0 + C * 500.0 * 1.1
        self.assertNotAlmostEqual(drv.samples[0][0], reckoner_only_estimate, places=2)

    def test_reckoner_is_never_called_for_dp_when_a_match_exists(self):
        """Direct proof, not just an inequality: `advance()`/`current_dp()`
        are not even reached for the `dp` value when `match is not None` --
        confirmed by patching both and asserting neither is invoked for the
        dp itself (last_known_dp/ms_since_last_known ARE still expected to
        be called -- AC-4 -- so only advance/current_dp are patched)."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True
        proc._handle_position({CAR_A: [0, 0, 55.0]}, _t(1.0))

        with mock.patch.object(reckoner, "advance", wraps=reckoner.advance) as m_advance, \
             mock.patch.object(reckoner, "current_dp", wraps=reckoner.current_dp) as m_current_dp:
            proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(1.1))

        m_advance.assert_not_called()
        m_current_dp.assert_not_called()


class Sample7RepurposingAndSoleReader(unittest.TestCase):
    """`sample[7]` (formerly the boolean `est`) is repurposed to hold
    `msSinceLastKnown`. AC-14's DTW gate is meant to be its ONE remaining
    reader in the module -- verified here by AST scan of the shipped
    source, not trusted from the implementer's own grep claim."""

    def test_sample7_holds_the_captured_ms_since_last_known_value(self):
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True

        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(3.0))

        drv = proc._drv(CAR_A)
        self.assertEqual(len(drv.samples[0]), 8, "sample tuple must still be 8 elements (unwidened)")
        self.assertAlmostEqual(drv.samples[0][7], 3000.0, places=3,
                                msg="sample[7] must be the ms-since-last-known captured AT collection time")

    def test_sample7_holds_ms_since_last_known_on_the_invalid_speed_zero_branch_too(self):
        """`_handle_car_data` builds `sample` in TWO places -- the `invalid`
        (speed==0, not in pit -> nulled channels) branch and the normal
        branch. WB3 changed sample[7] in BOTH; this covers the branch the
        other sample7 tests above don't reach (they all use nonzero speed)."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        drv = proc._drv(CAR_A)
        drv.activated = True
        drv.in_pit = False   # speed==0 while NOT in pit -> the `invalid` branch

        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=0)]}, _t(2.0))

        self.assertEqual(len(drv.samples), 1)
        sample = drv.samples[0]
        self.assertEqual(len(sample), 8)
        self.assertIsNone(sample[1], "speed must be nulled on the invalid (on-track dropout) branch")
        self.assertAlmostEqual(sample[7], 2000.0, places=3,
                                msg="sample[7] must still be the captured ms-since-last-known "
                                    "on the invalid-speed branch, not the old est flag")

    def test_sample7_never_a_boolean_zero_or_one_by_construction(self):
        """A quick sanity that the OLD boolean meaning is truly gone: a
        genuine ms-since-last-known value of exactly 0 or 1 would be
        indistinguishable from the old est=0/1 by VALUE alone -- so this
        test uses an elapsed time that could never be mistaken for a
        boolean (3000.0), confirming the field is read as a float
        magnitude, not truthiness, downstream (AC-14's own gate, tested
        separately, thresholds on `> 1000`, not `== 1`)."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner)
        proc._drv(CAR_A).activated = True
        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(3.0))
        self.assertGreater(proc._drv(CAR_A).samples[0][7], 1,
                            "test setup: elapsed ms must exceed 1, or this test can't distinguish "
                            "the old boolean meaning from the new float meaning")

    def test_sample7_has_exactly_one_reader_in_the_module_the_dtw_gate(self):
        """AST scan: every `Subscript` node in the shipped module indexing a
        literal `7` -- must be exactly one (the AC-14 DTW majority-count
        line), everywhere else touching sample tuples must stay within the
        first 7 elements (indices 0-6, or a slice like `s[:6]`/`s[:7]`)."""
        src = inspect.getsource(telemetry_processor_mod)
        tree = ast.parse(src)
        seven_index_lines = []

        class _Visitor(ast.NodeVisitor):
            def visit_Subscript(self, node):
                sl = node.slice
                if isinstance(sl, ast.Constant) and sl.value == 7:
                    seven_index_lines.append(node.lineno)
                self.generic_visit(node)

        _Visitor().visit(tree)
        # A single logical expression can contain more than one `[7]`
        # Subscript AST node (e.g. `s[7] is not None and s[7] > threshold`
        # reads the index twice on one line) -- "one reader" means one
        # LOCATION in the code, not one AST node, so distinct LINE numbers
        # is the right granularity here.
        distinct_lines = sorted(set(seven_index_lines))
        self.assertEqual(
            len(distinct_lines), 1,
            f"expected exactly one `[7]` index read (one code location -- the AC-14 DTW "
            f"gate) in the module, found at line(s) {seven_index_lines}",
        )

    def test_persisted_telemetry_lap_row_does_not_carry_the_8th_element(self):
        """AC-4's own note: `msSinceLastKnown`/`lastKnownDp` are LIVE-only,
        not persisted into the `telemetryLap` row shape -- `sample[7]` is
        dropped before the row reaches `out` (`_emit_lap`'s own construction:
        `[s[0], s[1], s[2], s[3], s[4], s[5], int(s[6] - start_ms)]`, 7
        elements). Verified end-to-end here, not just by reading the slice."""
        reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
        bus = _mock_bus()
        proc = _proc(bus, reckoner=reckoner, session_type="race")
        drv = proc._drv(CAR_A)
        drv.activated = True
        drv.crossings = [_t(0.0)]

        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(1.0))
        proc._handle_car_data({"Entries": [_entry(CAR_A, speed=250.0)]}, _t(2.0))
        proc._emit_lap(drv, 1, _t(0.0), _t(2.5))

        lap_calls = [c for c in bus.emitted_log if c[0] == f"telemetryLap:{CAR_A}:1"]
        self.assertEqual(len(lap_calls), 1)
        rows = lap_calls[0][1]
        self.assertTrue(rows, "test setup: must have produced at least one persisted row")
        for row in rows:
            self.assertEqual(len(row), 7, "a persisted telemetryLap row must have exactly 7 "
                                           "elements -- sample[7]/msSinceLastKnown is LIVE-only")


class DeterminismAC12(unittest.TestCase):
    def test_no_wall_clock_call_in_telemetry_processor_source(self):
        """Static (AST) guard, same technique as the sibling WB1/WB2 suites:
        no CALL to a wall-clock function appears anywhere in the module."""
        src = inspect.getsource(telemetry_processor_mod)
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

    def test_identical_car_data_sequence_produces_identical_samples_every_run(self):
        """Same input message sequence, same output, run twice on two
        independent (proc, reckoner) pairs."""
        def run() -> list:
            reckoner = _seeded_reckoner(CAR_A, start_ts=_t(0.0), start_dp=0.0)
            bus = _mock_bus()
            proc = _proc(bus, reckoner=reckoner)
            proc._drv(CAR_A).activated = True
            for i in range(1, 15):
                proc._handle_car_data({"Entries": [_entry(CAR_A, speed=200.0 + i)]}, _t(i * 0.3))
            return [tuple(s) for s in proc._drv(CAR_A).samples]

        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
