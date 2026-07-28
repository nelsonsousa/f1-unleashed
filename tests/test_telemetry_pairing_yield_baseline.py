"""Regression case for architecture-plan.md §A.7.1 / backend-synthesis.md G1 /
test-battery-plan.md WB-3 — "the test whose absence let c66d123 ship"
(architecture-plan.md §A.9.4).

`telemetry_processor.py`'s CarData/Position pairing (docstring: strict 1:1,
single-slot `pending_pos` per driver) silently discards anything that doesn't
pair. D7-B (requirement-spec.md §9.3, decided but not yet implemented) will
change this pairing to key on `utcTimestamp`. Before that lands, there must be
a baseline of what the CURRENT, WORKING code actually achieves — established
by `utils/scripts/measure_telemetry_pairing_baseline.py` and recorded in
`docs/telemetry_pairing_baseline.json` — so a future run of this same test
after D7-B lands can prove the change did not regress pairing yield, using the
REAL pipeline rather than a simulation of the pairing rule.

This test re-runs the real measurement (not a cached read of the JSON) against
the golden fixtures and asserts the measured yield has not regressed below the
recorded baseline, per fixture and overall. It is SKIPPED if the golden
fixture set (`regression/golden/`, a sibling directory of this repo, not part
of `repos/dev`) is not present, and if the baseline file has not yet been
established — both legitimate "cannot test this without infrastructure that
does not exist here" cases per rules/testing.md, not silently passed.

Cost/coverage trade-off, stated explicitly: measuring against the real
pipeline costs ~50-100s per fixture (six populated fixtures ~ 7-8 minutes
total). Running the full set on every CI invocation is expensive for a suite
this project otherwise runs in seconds. By default this test re-measures a
SUBSET (the smallest live + smallest CDN fixture — one of each source, since
architecture-plan.md's own §A.7.1 finding is that CDN and live pairing differ
structurally) which still exercises the pairing/regression logic end-to-end.
Set F1U_PAIRING_BASELINE_FULL=1 to re-measure every populated fixture (do this
before/after a D7-B implementation change, or in a dedicated slow-test CI
job — not on every commit).
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = REPO_ROOT.parent.parent / "regression" / "golden"
BASELINE_FILE = REPO_ROOT / "docs" / "telemetry_pairing_baseline.json"

sys.path.insert(0, str(REPO_ROOT / "utils" / "scripts"))


# Tolerance for float rounding / non-determinism sites elsewhere in the
# pipeline (see backend-synthesis.md §1.3 — the three unseeded set-iteration
# sites are in lap_timing/fia_stewards, not telemetry_processor, but this
# tolerance is deliberately non-zero rather than assuming telemetry_processor
# is unaffected by anything upstream).
YIELD_REGRESSION_TOLERANCE_PP = 0.5   # percentage points


def _load_baseline() -> dict:
    return json.loads(BASELINE_FILE.read_text())


def _smallest_case(cases: dict, suffix: str):
    """Smallest-by-live.jsonl-size populated case whose name ends `suffix`
    ('-live' or '-cdn'), or None."""
    candidates = [(name, path) for name, path in cases.items() if name.endswith(suffix)]
    if not candidates:
        return None
    return min(candidates, key=lambda kv: (kv[1] / "live.jsonl").stat().st_size)


@unittest.skipUnless(GOLDEN.exists(), f"golden fixture set not present at {GOLDEN}")
@unittest.skipUnless(BASELINE_FILE.exists(),
                      "no baseline recorded — run "
                      "utils/scripts/measure_telemetry_pairing_baseline.py first")
class TelemetryPairingYieldBaseline(unittest.IsolatedAsyncioTestCase):

    async def _measure(self, case: str, session_path: Path) -> dict:
        # Imported here (after the skip guards above have already run) so a
        # host without the app's runtime deps (e.g. numpy) still lets the
        # rest of the suite discover cleanly — only this test class pays for
        # the import.
        from measure_telemetry_pairing_baseline import measure_one
        import tempfile
        import shutil

        scratch = Path(tempfile.mkdtemp(prefix="f1u_pairing_test_"))
        os.environ["F1_DATA_HOME"] = str(scratch)
        (scratch / "tmp").mkdir(parents=True, exist_ok=True)
        try:
            return await measure_one(case, session_path, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    async def test_pairing_yield_has_not_regressed_below_baseline(self):
        from measure_telemetry_pairing_baseline import _discover_cases

        baseline = _load_baseline()
        cases = _discover_cases(GOLDEN)
        self.assertTrue(cases, f"no populated fixtures under {GOLDEN}")

        if os.environ.get("F1U_PAIRING_BASELINE_FULL"):
            to_run = cases
        else:
            picked = {}
            for suffix in ("-live", "-cdn"):
                found = _smallest_case(cases, suffix)
                if found:
                    picked[found[0]] = found[1]
            to_run = picked or cases  # fall back to everything if naming ever changes

        self.assertTrue(to_run, "no fixtures selected to measure")

        failures = []
        for case, session_path in to_run.items():
            with self.subTest(case=case):
                base = baseline["per_fixture"].get(case)
                self.assertIsNotNone(
                    base, f"{case} has no recorded baseline in {BASELINE_FILE} — "
                    "re-run measure_telemetry_pairing_baseline.py to establish one "
                    "before trusting a comparison against it")
                self.assertFalse(base["build_failed"],
                                  f"{case}'s recorded baseline itself failed to build")

                result = await self._measure(case, session_path)

                self.assertFalse(
                    result["build_failed"],
                    f"{case}: pipeline build failed during re-measurement "
                    f"(baseline build had succeeded)")
                self.assertGreater(
                    result["cardata_candidates"], 0,
                    f"{case}: no CarData candidates found — fixture or filter broken, "
                    "not a real pairing measurement")

                floor = base["yield_pct"] - YIELD_REGRESSION_TOLERANCE_PP
                if result["yield_pct"] < floor:
                    failures.append(
                        f"{case}: yield {result['yield_pct']}% < baseline "
                        f"{base['yield_pct']}% - {YIELD_REGRESSION_TOLERANCE_PP}pp "
                        f"tolerance (floor {floor:.4f}%) — paired="
                        f"{result['paired']}/{result['cardata_candidates']}"
                    )

        self.assertFalse(failures, "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
