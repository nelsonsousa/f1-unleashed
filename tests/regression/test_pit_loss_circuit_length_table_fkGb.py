"""RED GATE — Trello card FfkO6ELK (Bug B split, pit-loss circuit-length table staleness).

Evidence: docs/artifacts/2026-08-20-080-pit-loss-circuit-length-table-fix/file-impact-map.md.

`app/analysis/pit_loss_estimate.py`'s `compute()` gates on a hand-maintained
`CIRCUIT_LENGTH_M` dict (9 entries, last updated through round 1289/Silverstone). Any
circuit not present in that dict short-circuits at the very top of `compute()` (before any
telemetry/DB read) and returns `None`, silently, with the event logged at INFO rather than
WARNING. This currently blocks the pre-race pit-loss estimate for every session at
Spa-Francorchamps and Budapest (Hungarian GP), and will silently recur at every future new
circuit (Zandvoort next) with no visible signal that anything is wrong.

All tests below are written against the CURRENT (unfixed) code at `test`@63ae958 and are
expected to FAIL (or pass for the wrong/silent reason) for the reasons stated in each
docstring. See test-plan.md in this task's artifact directory for the exact captured
red-gate failure output.
"""
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from app.analysis.pit_loss_estimate import CIRCUIT_LENGTH_M, compute


class CircuitLengthTableMissingEntriesTests(unittest.TestCase):
    """Defect (primary): CIRCUIT_LENGTH_M is missing both circuits that currently need a
    pit-loss estimate. compute()'s gate at line 234 (`if circuit not in CIRCUIT_LENGTH_M`)
    trips for both before any telemetry logic runs — this is the actual defect surface, not
    a downstream symptom, so a bare dict-membership assertion encodes the bug directly and
    needs no DB/telemetry fixture at all.
    """

    def test_circuit_length_table_has_spa_francorchamps(self):
        """Against unfixed code: 'Spa-Francorchamps' is absent from CIRCUIT_LENGTH_M, so
        this fails with AssertionError. Once the fix adds the entry, this passes."""
        self.assertIn(
            "Spa-Francorchamps", CIRCUIT_LENGTH_M,
            "CIRCUIT_LENGTH_M is missing 'Spa-Francorchamps' -- compute() will "
            "unconditionally short-circuit to None for every Spa session",
        )

    def test_circuit_length_table_has_budapest(self):
        """Against unfixed code: 'Budapest' is absent from CIRCUIT_LENGTH_M, so this fails
        with AssertionError. NOTE: the correct key is the raw, unmapped location string
        'Budapest' (what `_circuit_of()` actually returns), NOT 'Hungaroring' (the
        CIRCUIT_NAME_MAP SVG-asset name, a different key space entirely -- see
        file-impact-map.md section 3). A fix that adds 'Hungaroring' instead would leave
        this test failing."""
        self.assertIn(
            "Budapest", CIRCUIT_LENGTH_M,
            "CIRCUIT_LENGTH_M is missing 'Budapest' -- compute() will "
            "unconditionally short-circuit to None for every Budapest/Hungaroring session",
        )

    def test_compute_returns_none_for_spa_today(self):
        """Confirms the observable symptom, not just the table's contents: compute() itself
        returns None for a Spa-Francorchamps session path today, purely because of the gate
        at line 234 -- no DB, no telemetry, no mocking needed, since the gate runs before
        any of that. Against unfixed code this assertion PASSES (compute() really does
        return None) -- captured in test-plan.md as the "passes for the wrong/silent
        reason" evidence: nothing here tells a caller *why* it came back None, which is
        exactly the meta-cause this task also fixes (WARNING log, see below)."""
        session_path = Path("data/livetiming_cache/2026/1290_Spa-Francorchamps/11338_Qualifying")
        self.assertIsNone(compute(session_path))

    def test_compute_returns_none_for_budapest_today(self):
        """Same as above, for Budapest. Against unfixed code this PASSES silently."""
        session_path = Path("data/livetiming_cache/2026/1295_Budapest/11390_Qualifying")
        self.assertIsNone(compute(session_path))


class UnrecognizedCircuitLogsWarningNotSilentlyTests(unittest.TestCase):
    """Defect (meta-cause, ranked recommendation #5 in the source investigation): today's
    unrecognized-circuit path logs at INFO, not WARNING, so the exact same silent-freeze
    will recur at every future new circuit with no operational visibility. This test uses
    an arbitrary unlisted circuit name (not Spa or Budapest, so it survives even after the
    table fix lands) to prove the log-level defect independently of the table-entry fix.
    """

    LOGGER_NAME = "app.analysis.pit_loss_estimate"

    def test_unrecognized_circuit_emits_warning_log(self):
        """Against unfixed code, the call site is `logger.info(...)`, one level below
        WARNING. `assertLogs(..., level="WARNING")` raises AssertionError when no record at
        WARNING or above is emitted -- this is expected to fail cleanly against unfixed
        code (a real, observable red-gate failure), not a false negative."""
        session_path = Path("data/livetiming_cache/2026/9999_Zandvoort/54321_Qualifying")
        with self.assertLogs(self.LOGGER_NAME, level="WARNING") as cm:
            result = compute(session_path)
        self.assertIsNone(result)
        self.assertTrue(
            any("Zandvoort" in record for record in cm.output),
            f"expected a WARNING mentioning the unrecognized circuit, got: {cm.output}",
        )

    # NOTE (implementer, post-fix): this class originally also had
    # `test_unrecognized_circuit_today_only_logs_at_info`, a canary that positively
    # confirmed today's (pre-fix) INFO-level logging as red-gate evidence. Its own docstring
    # said explicitly: "if it is [WARNING], this red-gate test is stale and needs to be
    # re-derived" -- i.e. it was designed to fail, by intent, the moment the WARNING fix
    # landed, and was never meant to remain in the green suite. Removed here rather than left
    # to fail permanently; its captured red-gate evidence is preserved verbatim in
    # test-plan.md, and the WARNING-path acceptance criterion it existed to corroborate is
    # independently covered by `test_unrecognized_circuit_emits_warning_log` above.


class FullComputeSucceedsOnceCircuitIsRecognizedTests(unittest.TestCase):
    """Stronger regression test proving compute() runs all the way through the real
    geometry pipeline and returns an actual pit-loss estimate dict, not just that the gate
    stops tripping. `_read()` and `find_prior_session()` are mocked with hand-built
    synthetic telemetry/status sequences (per file-impact-map.md section 5's recommended
    seam) since no real telemetry fixture exists in-repo for these circuits.

    Against UNFIXED code this fails at the very first line of compute() (the
    CIRCUIT_LENGTH_M gate) regardless of what _read()/find_prior_session() are mocked to
    return -- proving the gate, not the mocks, is what's broken. Once the table fix lands,
    this exercises _session_spans/_pit_windows/_stationary/_zone_dp for real and checks the
    resulting dict's shape.
    """

    def _synthetic_pit_stop(self, car_num="44"):
        """One clean pit-lane visit: approach at track speed, a controlled zone bounded by
        the pit limit (80 km/h), a stationary tyre change, and departure back to track
        speed. dp values are a monotonically increasing lap-distance percentage."""
        status_seq = [
            (0, "TRACK"),
            (1000, "PIT"),
            (6000, "TRACK"),
        ]
        telemetry_seq = [
            (-500, 250, 5.0),
            (0, 200, 10.0),
            (500, 150, 20.0),
            (800, 90, 30.0),
            (900, 80, 32.0),
            (950, 80, 33.0),
            (1000, 80, 35.0),
            (1300, 60, 40.0),
            (1600, 30, 45.0),
            (2000, 0, 50.0),
            (2200, 0, 50.0),
            (2400, 0, 50.0),
            (2600, 0, 50.0),
            (2800, 0, 50.0),
            (3000, 0, 50.0),
            (3300, 30, 55.0),
            (3600, 60, 60.0),
            (3900, 80, 63.0),
            (3950, 80, 64.0),
            (4000, 80, 65.0),
            (4300, 90, 70.0),
            (4600, 150, 80.0),
            (5000, 200, 90.0),
            (6000, 250, 95.0),
        ]
        return {car_num: telemetry_seq}, {car_num: status_seq}

    def test_compute_returns_a_real_estimate_dict_for_spa_once_recognized(self):
        """Fails against unfixed code: compute() short-circuits to None at the
        CIRCUIT_LENGTH_M gate before _read() (mocked here) is ever even called -- so the
        synthetic telemetry below is never touched. Once the fix adds
        'Spa-Francorchamps' to the table, this test exercises the real geometry pipeline
        and checks a plausible, well-shaped result dict comes back."""
        session_path = Path("data/livetiming_cache/2026/1290_Spa-Francorchamps/11338_Qualifying")
        tel, st = self._synthetic_pit_stop()
        with patch("app.analysis.pit_loss_estimate._read", return_value=(tel, st)), \
             patch("app.analysis.pit_loss_estimate.find_prior_session", return_value=None):
            result = compute(session_path)

        self.assertIsNotNone(
            result,
            "compute() still returned None -- either the CIRCUIT_LENGTH_M gate has not "
            "been fixed for 'Spa-Francorchamps', or the synthetic telemetry failed to "
            "clear the geometry pipeline",
        )
        self.assertEqual(result["circuit"], "Spa-Francorchamps")
        self.assertEqual(result["metric"], "pit_lane_transit")
        self.assertGreater(result["pit_lane_transit_s"], 0)
        self.assertGreater(result["pit_lane_length_m"], 0)
        self.assertEqual(result["speed_limit_kmh"], 80)


if __name__ == "__main__":
    unittest.main()
