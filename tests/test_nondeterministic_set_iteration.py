"""WB2 — RED GATE regression test (unfixed): non-deterministic driver-order
emits caused by iterating a Python `set()` of driver-number strings.

CPython randomizes string hash order per PROCESS (no PYTHONHASHSEED pinned
anywhere in this repo — confirmed by grep). A `set[str]` therefore iterates
in an order that depends on the process's hash seed, not on insertion order
or driver-number order. Rebuilding the IDENTICAL capture with the IDENTICAL
code in two separate process runs can, and does, produce a different
driver-emit order each time. That makes it impossible to tell a genuine
regression in output *order/content* apart from this noise.

Three call sites are implicated:

  app/processing/processors/lap_timing_processor.py:155
      `for num in (self._roster or set(self._nol) | set(self._laps)):`
      NOTE the trap: self._roster is ITSELF a `set[str]` (see its
      declaration, `self._roster: set[str] = set()`), so the "primary"
      branch (when the roster is non-empty, the common case) is ALSO
      non-deterministic, not just the `set(...) | set(...)` fallback.

  app/processing/processors/lap_timing_processor.py:219
      `for num in changed:` where `changed = set()` accumulated per message.

  app/processing/processors/fia_stewards_processor.py:201
      `for num in set(by_driver) | set(self._last_emit):`

This test does NOT rely on relative timing or on hash randomization
happening to differ "by luck" between two runs (which would make the test
itself flaky). Instead it pins two DIFFERENT, FIXED values of
PYTHONHASHSEED via subprocess and re-runs the *exact same* scenario against
each. Two different fixed seeds deterministically reproduce two different,
stable hash orderings for a large enough set of driver numbers (real F1
grids: ~20 cars) — so the comparison below is itself 100% reproducible
across repeated `unittest` runs, while still directly proving the process
non-determinism the bug describes: identical capture, identical code,
different process (= different seed) -> different emitted order.

Expected result against the CURRENT (unfixed) code: FAIL (orders differ).
Expected result once the affected iterations are pinned to a deterministic
order, e.g. `sorted(...)`: PASS (orders are identical regardless of seed).
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A realistic full F1 grid's worth of driver numbers, deliberately NOT
# supplied in sorted order to the roster/message builders below, so a
# passing (fixed) implementation must be actively sorting/ordering them --
# not just accidentally reproducing insertion order.
_DRIVER_NUMS = [
    "1", "2", "3", "4", "5", "11", "14", "16", "18", "20",
    "22", "23", "24", "27", "31", "44", "55", "63", "77", "81",
]

_CT_ISO = "2026-07-17T15:30:00+00:00"

_LAP_TIMING_ROSTER_SCRIPT = f"""
import sys
from datetime import datetime
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.lap_timing_processor import LapTimingProcessor

CT = datetime.fromisoformat({_CT_ISO!r})
NUMS = {_DRIVER_NUMS!r}

bus = SessionMessageBus()
proc = LapTimingProcessor(bus, "race")
proc.subscribe()
seen = []
bus.on('*', lambda topic, data, ct: seen.append(topic)
       if topic.startswith("driverLaps:") else None)
# Populates self._roster (a set[str]) -- line 155's PRIMARY branch.
bus.emit("driverList", {{n: {{}} for n in NUMS}}, CT)
# First SessionStatus Started in a race -> the lights-out lap-1 emit that
# iterates self._roster directly (line 155).
bus.emit("SessionStatus", {{"Status": "Started"}}, CT)
sys.stdout.write(",".join(seen))
"""

_LAP_TIMING_CHANGED_SCRIPT = f"""
import sys
from datetime import datetime
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.lap_timing_processor import LapTimingProcessor

CT = datetime.fromisoformat({_CT_ISO!r})
NUMS = {_DRIVER_NUMS!r}

bus = SessionMessageBus()
proc = LapTimingProcessor(bus, "practice")
proc.subscribe()
bus.emit("SessionStatus", {{"Status": "Started"}}, CT)
seen = []
bus.on('*', lambda topic, data, ct: seen.append(topic)
       if topic.startswith("driverLaps:") else None)
lines = {{n: {{"NumberOfLaps": 2,
             "LastLapTime": {{"Value": "1:30.000", "PersonalFastest": True}}}}
         for n in NUMS}}
# All drivers complete a lap in ONE TimingData message -> `changed` (a
# set[str], line 219) is iterated once for all of them.
bus.emit("TimingData", {{"Lines": lines}}, CT)
sys.stdout.write(",".join(seen))
"""

_FIA_STEWARDS_SCRIPT = f"""
import sys
from datetime import datetime, timedelta
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.fia_stewards_processor import FiaStewardsProcessor

CT = datetime.fromisoformat({_CT_ISO!r})
NUMS = {_DRIVER_NUMS!r}

bus = SessionMessageBus()
proc = FiaStewardsProcessor(bus, "race")
proc.subscribe()
bus.emit("SessionInfo", {{}}, CT)
# Push a waved blue flag for every driver, one RCM at a time (each expires
# 10s after issuance).
for i, n in enumerate(NUMS):
    bus.emit("RaceControlMessages", {{"Messages": {{str(i): {{
        "Category": "Flag", "Flag": "BLUE",
        "Message": f"BLUE FLAG FOR CAR {{n}} (ABC)"}}}}}}, CT)

seen = []
bus.on('*', lambda topic, data, ct: seen.append(topic)
       if topic.startswith("driverPenalties:") else None)
# Advance the clock past every driver's expiry in one tick -> a SINGLE
# _emit() call where `set(by_driver) | set(self._last_emit)` (line 201)
# newly clears all 20 drivers at once, exposing its iteration order.
bus.emit("clock", {{}}, CT + timedelta(seconds=11))
sys.stdout.write(",".join(seen))
"""


def _run_in_subprocess(script: str, hash_seed: str) -> list[str]:
    """Run `script` as a fresh child process pinned to `hash_seed`, and
    return the ordered list of emitted topics it printed to stdout.

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
    return out.split(",") if out else []


class NondeterministicSetIterationOrder(unittest.TestCase):
    """Same capture, same code, two different (fixed, reproducible) process
    hash seeds -> the emitted driver order must be identical for the output
    to be trustworthy as a regression-testing negative control. It is not,
    today, because each affected line iterates a `set[str]`."""

    SEED_A = "12345"
    SEED_B = "998877"

    def test_lap_timing_roster_emit_order_is_process_dependent(self):
        """lap_timing_processor.py:155 -- the PRIMARY branch (self._roster
        itself is a set[str], not just the set-union fallback)."""
        order_a = _run_in_subprocess(_LAP_TIMING_ROSTER_SCRIPT, self.SEED_A)
        order_b = _run_in_subprocess(_LAP_TIMING_ROSTER_SCRIPT, self.SEED_B)

        # Sanity: both runs saw every driver, just possibly in a different
        # order -- proves this is purely an ORDERING defect, not a data-loss
        # defect.
        self.assertEqual(
            sorted(order_a), sorted(order_b),
            "sanity check failed: the two runs did not even emit the same "
            "SET of drivers -- this test no longer isolates the ordering bug",
        )
        self.assertEqual(
            order_a, order_b,
            "lap_timing_processor.py:155 iterates self._roster (a set[str]) "
            "directly; identical input processed in two separate processes "
            "with different (but fixed) PYTHONHASHSEED values produced "
            f"different driverLaps emit order:\nseed={self.SEED_A}: {order_a}\n"
            f"seed={self.SEED_B}: {order_b}",
        )

    def test_lap_timing_changed_drivers_emit_order_is_process_dependent(self):
        """lap_timing_processor.py:219 -- `for num in changed:` where
        `changed` is accumulated as a `set()` in _handle_timing."""
        order_a = _run_in_subprocess(_LAP_TIMING_CHANGED_SCRIPT, self.SEED_A)
        order_b = _run_in_subprocess(_LAP_TIMING_CHANGED_SCRIPT, self.SEED_B)

        self.assertEqual(
            sorted(order_a), sorted(order_b),
            "sanity check failed: the two runs did not even emit the same "
            "SET of drivers -- this test no longer isolates the ordering bug",
        )
        self.assertEqual(
            order_a, order_b,
            "lap_timing_processor.py:219 iterates `changed` (a set[str]) "
            "built during _handle_timing; identical input processed in two "
            "separate processes with different (but fixed) PYTHONHASHSEED "
            f"values produced different driverLaps emit order:\n"
            f"seed={self.SEED_A}: {order_a}\nseed={self.SEED_B}: {order_b}",
        )

    def test_fia_stewards_emit_order_is_process_dependent(self):
        """fia_stewards_processor.py:201 --
        `for num in set(by_driver) | set(self._last_emit):`"""
        order_a = _run_in_subprocess(_FIA_STEWARDS_SCRIPT, self.SEED_A)
        order_b = _run_in_subprocess(_FIA_STEWARDS_SCRIPT, self.SEED_B)

        self.assertEqual(
            sorted(order_a), sorted(order_b),
            "sanity check failed: the two runs did not even emit the same "
            "SET of drivers -- this test no longer isolates the ordering bug",
        )
        self.assertEqual(
            order_a, order_b,
            "fia_stewards_processor.py:201 iterates "
            "`set(by_driver) | set(self._last_emit)`; identical input "
            "processed in two separate processes with different (but "
            "fixed) PYTHONHASHSEED values produced different "
            f"driverPenalties emit order:\nseed={self.SEED_A}: {order_a}\n"
            f"seed={self.SEED_B}: {order_b}",
        )


if __name__ == "__main__":
    unittest.main()
