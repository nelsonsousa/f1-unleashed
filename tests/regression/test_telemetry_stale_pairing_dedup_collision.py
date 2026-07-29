"""Regression (fix-attempt 3, 2026-07-28): D7-B's `_nearest_preceding_pos`
never consumed the matched position, so one buffered position could pair
with an UNBOUNDED number of later CarData entries. The module docstring
defended this as correct for the `dp` VALUE ("the car's position genuinely
hasn't changed between them") — true — but it also meant several genuinely
distinct CarData samples were stored with byte-identical `[dp, ..., t_ms]`
in `drv.samples` (the completed-lap trace), differing only in channel
readings. Production's pre-redesign single-slot model never had this
problem: it clears `pending_pos` immediately after a CarData entry consumes
it, so a position pairs AT MOST ONCE.

Measured against real Spa 2026 Qualifying data (see
docs/artifacts/2026-07-28-016-telemetry-downstream-reconciliation/
data-investigation.md, the investigation that corrected this attempt's
first-pass fix): 69.4% of committed `telemetryLap` samples carried a
timestamp duplicated from another sample in the same lap (production:
0.02%), collapsing distinct telemetry resolution to ~37.7% of production's
(on-track distinct-sample rate 1.147 Hz vs production's 3.065 Hz).

NOTE — this test intentionally does NOT assert full parity with
production's overall distinct-sample RATE. `_nearest_unconsumed_pos`
restores the "consume at most once" invariant (this test's actual subject),
which eliminates the duplicate-timestamp defect (94.8% -> 0%, see the
fix-attempt-3 verification report) but — confirmed empirically, not
assumed — does NOT by itself close the remaining pairing-YIELD gap to
production (on-track rate unchanged at 1.147 Hz with or without this fix).
That gap requires restoring some form of bounded chronological
reordering/lookahead across the two streams (removed pipeline-wide by this
redesign, §2.3) and is out of this bounded final attempt's scope — see
`docs/artifacts/2026-07-28-014-wb5-6-pipeline-redesign-fix2/escalation.md`.

RED GATE (rules/testing.md): both tests below were run against the CURRENT,
unfixed code (`_nearest_preceding_pos`, no consumption) and observed to FAIL
— see verification-report.md for the captured failures.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor

NUM = "44"


class _Harness(unittest.TestCase):
    """Wires a REAL SessionMessageBus, a REAL TelemetryProcessor, and
    preprocessor.py's REAL, unmodified `_capture_output` persist sink — the
    exact components whose interaction produced the real-data telemetry
    defect. Bypasses only the raw-file/gate/normalizer machinery (irrelevant
    here and heavyweight to fixture — see test_telemetry_pairing_yield_d7b.py
    for the same convention) by driving the bus directly with the already-
    decoded `"position"`/`"CarData.z"` topic shapes those components consume.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            self.p = SessionPreProcessor(self.sess, "practice")
        self.p._db.open()
        self.p._start_time = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        self.p._scheduled_start_utc = None
        self.p._bus.set_persist_sink(self.p._capture_output)
        self.tp = TelemetryProcessor(self.p._bus, "practice")
        self.tp.subscribe()
        drv = self.tp._drv(NUM)
        drv.activated = True   # so samples are stored (drv.samples), not just live-passthrough

    def tearDown(self):
        self.p._db.close()

    def _car_data(self, ts: datetime, speed: int = 220) -> dict:
        return {"Entries": [{
            "Utc": ts.isoformat().replace("+00:00", "Z"),
            "Cars": {NUM: {"Channels": {"0": 9000, "2": speed, "3": 5, "4": 60, "5": 0}}},
        }]}


class OnePositionConsumedAtMostOnce(_Harness):
    def test_five_carData_samples_against_one_stale_position_only_the_first_pairs(self):
        """ONE position sample, then five CarData entries 250ms apart with NO
        further position update (the batched-arrival gap D7-B's docstring
        describes) — the exact shape that produced the real-data duplicate-
        timestamp defect. Only the FIRST CarData entry has an eligible
        (unconsumed) position — it alone must pair (dp=42.0, not re-paired
        against the same stale position for a later entry).

        AC-3 (requirement-spec.md, this repo's later Candidate A work):
        the other four CarData entries are no longer silently SKIPPED —
        they now PERSIST with dp=None (channels still captured), the
        emit-unpaired behavior AC-3 deliberately introduces. This test's
        invariant (a position pairs AT MOST ONCE — the actual subject here,
        the duplicate-timestamp defect fix-attempt-3 fixed) is unaffected by
        that later change; only the total row count and the trailing four
        rows' dp values need updating to match.
        """
        base = self.p._start_time
        self.p._bus.emit("position", {NUM: [0, 0, 42.0]}, base)

        n = 5
        for i in range(1, n + 1):
            ts = base + timedelta(milliseconds=250 * i)
            self.p._bus.emit("CarData.z", self._car_data(ts), ts)

        drv = self.tp._drv(NUM)
        self.assertEqual(
            len(drv.samples), n,
            f"Expected {n} stored samples (1 paired + {n - 1} unpaired-but-"
            f"persisted under AC-3's emit-unpaired rule) but got "
            f"{len(drv.samples)}.",
        )
        # The FIRST stored sample is the only one with an eligible unconsumed
        # position (dp=42.0, from the single buffered position); a position
        # must never be reused for a later CarData sample.
        self.assertEqual(drv.samples[0][0], 42.0)
        for later_sample in drv.samples[1:]:
            self.assertIsNone(
                later_sample[0],
                "a stale, already-consumed position must never be reused for "
                "a later CarData sample — it must persist as dp=None (AC-3), "
                "not re-pair against the same stale position",
            )

    def test_no_two_stored_samples_share_a_timestamp_across_a_longer_batched_run(self):
        """Same shape, longer run (8 CarData against 1 position): whatever
        DOES get stored must never contain two samples with the same
        abs_ms (`sample[6]`) — the mechanical form of the invariant
        production always held (`pending_pos = None` after each use), and
        the property `docs/artifacts/.../data-investigation.md` §11 item 5
        flagged as worth making mechanical."""
        base = self.p._start_time
        self.p._bus.emit("position", {NUM: [0, 0, 10.0]}, base)
        for i in range(1, 9):
            ts = base + timedelta(milliseconds=200 * i)
            self.p._bus.emit("CarData.z", self._car_data(ts, speed=200 + i), ts)

        drv = self.tp._drv(NUM)
        t_ms_values = [s[6] for s in drv.samples]
        self.assertEqual(
            len(t_ms_values), len(set(t_ms_values)),
            f"Stored samples must never share a timestamp (abs_ms) — found "
            f"duplicates in {t_ms_values}.",
        )


class MultiplePositionsAllowMultiplePairingsWithoutReuse(_Harness):
    def test_each_of_three_positions_pairs_with_a_distinct_carData_entry(self):
        """Three positions genuinely interleaved with three CarData entries
        (each CarData's own timestamp falls strictly after its OWN nearest
        position and strictly before the next one) — each CarData must pair
        with its OWN, distinct position; no position may be reused for a
        later CarData once consumed. This is the genuine-interleaving case
        D7-B's buffer is meant to handle correctly (as opposed to the
        batched-with-no-new-position case in `OnePositionConsumedAtMostOnce`
        above)."""
        base = self.p._start_time
        dps = [10.0, 10.5, 11.0]
        for i, dp in enumerate(dps):
            pos_ts = base + timedelta(milliseconds=100 * i)
            self.p._bus.emit("position", {NUM: [0, 0, dp]}, pos_ts)
            car_ts = pos_ts + timedelta(milliseconds=50)
            self.p._bus.emit("CarData.z", self._car_data(car_ts, speed=200 + i), car_ts)

        drv = self.tp._drv(NUM)
        self.assertEqual(len(drv.samples), 3)
        self.assertEqual([s[0] for s in drv.samples], dps)


if __name__ == "__main__":
    unittest.main()
