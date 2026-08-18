"""Trello Kuryh6y4 — RED GATE regression test (unfixed): non-deterministic Q3
`dashAutoSelect` tie-break caused by iterating a Python `set()` of driver-number
strings in `DashboardAutoSelectProcessor._pick_q3`.

Discovered during WB-1's review
(docs/artifacts/2026-08-17-047-wb1-pipeline-redesign-resume/review-findings.md):
two builds of the exact same unmodified code produced different golden-fixture
output (515,799/99 rows vs 515,802/102 rows on the `shanghai-sq-cdn` fixture)
purely from hash-seed randomization; `HOLD_MS` hysteresis then amplifies one
flipped pick into a visible, persisted downstream difference.

This is the same bug class WB2 already fixed elsewhere in the pipeline (see
tests/test_nondeterministic_set_iteration.py) -- this instance was missed.

Root cause (app/processing/processors/dashboard_autoselect_processor.py,
`_pick_q3`):

    top5 = {n for n, p in self._pos.items() if isinstance(p, int) and p <= 5 and not fin.get(n)}
    top5 |= {n for n, p in self._pred.items() if isinstance(p, int) and p <= 5 and not fin.get(n)}
    by_dp = lambda ns: sorted(ns, key=lambda n: self._dp.get(n, -1.0), reverse=True)
    prime = by_dp([n for n in top5 if n in self._dp])

`top5` is a `set[str]`. When two or more drivers in `top5` are tied on track %
(`self._dp`), Python's `sorted()` is stable, so which driver wins the tie
depends entirely on the ITERATION ORDER of `top5` -- and CPython randomizes
`set[str]` iteration order per process (no PYTHONHASHSEED pinned anywhere in
this repo). Rebuilding the IDENTICAL capture with the IDENTICAL code in two
separate process runs can, and does, produce a different `dashAutoSelect` pick
each time.

This test does NOT rely on relative timing or on hash randomization happening
to differ "by luck" between two runs (which would make the test itself
flaky). Instead it pins two DIFFERENT, FIXED values of PYTHONHASHSEED via
subprocess (the same two seeds already used in
tests/test_nondeterministic_set_iteration.py for the analogous WB2 bug class)
and re-runs the exact same scenario against each. Two different fixed seeds
deterministically reproduce two different, stable hash orderings for this
5-driver tie -- so the comparison below is itself 100% reproducible across
repeated runs, while still directly proving the process non-determinism the
bug describes: identical capture, identical code, different process (=
different seed) -> different `dashAutoSelect` pick.

Expected result against the CURRENT (unfixed) code: FAIL (picks differ).
Expected result once `_pick_q3`'s set iteration is pinned to a deterministic
order (e.g. `sorted(top5)` before feeding it to `by_dp`, mirroring the WB2
fix): PASS (picks are identical regardless of seed).
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Five drivers, deliberately not fed in sorted order, all tied on track %
# and all inside the Q3 top-5 (positions P1-P5) -- so a passing (fixed)
# implementation must be actively ordering the tie-break, not just
# accidentally reproducing insertion order.
_DRIVER_NUMS = ["4", "1", "5", "2", "3"]

_CT_ISO = "2026-07-17T15:30:00+00:00"

_PICK_Q3_SCRIPT = f"""
import sys
from datetime import datetime
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.dashboard_autoselect_processor import DashboardAutoSelectProcessor

CT = datetime.fromisoformat({_CT_ISO!r})
NUMS = {_DRIVER_NUMS!r}

bus = SessionMessageBus()
proc = DashboardAutoSelectProcessor(bus, "qualifying")
proc.subscribe()

seen = []
bus.on("dashAutoSelect", lambda data, ct: seen.append(list(data)))

# All drivers tied on track % (same coord[2]) and all classed P1-P5, so all
# five land in _pick_q3's `top5` set with an identical sort key -- the tie
# the sorted()/set-iteration-order bug is exposed by.
bus.emit("position", {{n: [0.0, 0.0, 42.0] for n in NUMS}}, CT)
bus.emit("standings", {{"drivers": [{{"num": n, "position": i + 1}} for i, n in enumerate(NUMS)]}}, CT)
bus.emit("qualifyingPart", 3, CT)
# First SessionStatus Started -> _last was None, so the pick emits
# immediately (no HOLD_MS delay to wait out).
bus.emit("sessionInfo", {{"sessionStatus": "Started"}}, CT)

sys.stdout.write(",".join(seen[0]) if seen else "NONE")
"""


def _run_in_subprocess(script: str, hash_seed: str) -> list[str]:
    """Run `script` as a fresh child process pinned to `hash_seed`, and
    return the emitted dashAutoSelect pair it printed to stdout.

    A fresh subprocess is required (not just re-seeding random.Random)
    because PYTHONHASHSEED is read once at interpreter start-up -- it is
    literally the mechanism the bug report describes ("two separate
    process runs").
    """
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"subprocess (PYTHONHASHSEED={hash_seed}) failed:\n{result.stderr}"
        )
    out = result.stdout.strip()
    if out == "NONE" or not out:
        return []
    return out.split(",")


class DashAutoSelectPickQ3NondeterministicTieBreak(unittest.TestCase):
    """Same capture, same code, two different (fixed, reproducible) process
    hash seeds -> the Q3 dashAutoSelect pick must be identical for the
    output to be trustworthy. It is not, today, because `_pick_q3` builds
    and iterates a `set[str]` (`top5`) whose order decides ties."""

    SEED_A = "12345"
    SEED_B = "998877"

    def test_pick_q3_tie_break_is_process_dependent(self):
        pick_a = _run_in_subprocess(_PICK_Q3_SCRIPT, self.SEED_A)
        pick_b = _run_in_subprocess(_PICK_Q3_SCRIPT, self.SEED_B)

        # Sanity: both runs must actually have produced a 2-driver pick, and
        # from the same tied candidate pool -- proves this is purely an
        # ORDERING/tie-break defect, not a data-loss or "nobody picked"
        # defect that would trivially make the two lists "equal" (both
        # empty) for the wrong reason.
        self.assertEqual(len(pick_a), 2, f"seed={self.SEED_A} produced {pick_a!r}, expected a 2-driver pick")
        self.assertEqual(len(pick_b), 2, f"seed={self.SEED_B} produced {pick_b!r}, expected a 2-driver pick")
        self.assertTrue(
            set(pick_a) <= set(_DRIVER_NUMS) and set(pick_b) <= set(_DRIVER_NUMS),
            "sanity check failed: pick contained a driver outside the tied "
            f"candidate pool -- seed={self.SEED_A}: {pick_a}, seed={self.SEED_B}: {pick_b}",
        )

        self.assertEqual(
            pick_a, pick_b,
            "dashboard_autoselect_processor.py's _pick_q3 builds `top5` as a "
            "set[str] and feeds it straight into a stable sort keyed on "
            "track %% (self._dp); when drivers tie on track %% (as all five "
            "do here), the winner depends on set iteration order. Identical "
            "input processed in two separate processes with different (but "
            "fixed) PYTHONHASHSEED values produced a different Q3 "
            f"dashAutoSelect pick:\nseed={self.SEED_A}: {pick_a}\n"
            f"seed={self.SEED_B}: {pick_b}",
        )


if __name__ == "__main__":
    unittest.main()
