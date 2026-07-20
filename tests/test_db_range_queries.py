"""B04 wqMnjDSk / fiodUrGN: the seek history queries switched from LIKE 'prefix%'
(full scan) to a half-open range predicate (index seek), and an offset_ms index
was added. Pins that the range predicate returns EXACTLY the same rows as LIKE —
in particular it must exclude a sibling prefix (driverLapClassification: must not
match driverLaps:) — respects the offset cutoff, and that the index exists.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.database import SessionDatabase


class DbRangeQueries(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = SessionDatabase(self.dir, db_path=self.dir / "t.db")
        self.db.open()
        self.db.save_messages([
            (100, "", "driverLaps:44", json.dumps({"lap": 1})),
            (200, "", "driverLaps:44", json.dumps({"lap": 2})),
            (150, "", "driverLaps:1", json.dumps({"lap": 1})),
            # sibling prefix sharing "driverLap" — must NOT match "driverLaps:"
            (120, "", "driverLapClassification:44", json.dumps({"x": 1})),
            (130, "", "telemetryLap:44:1", json.dumps([1, 2, 3])),
            (140, "", "raceControlMessage", json.dumps({"m": "x"})),
        ])

    def tearDown(self):
        self.db.close()

    def test_prefix_history_matches_only_the_prefix_in_offset_order(self):
        res = self.db.get_topic_prefix_history("driverLaps:", 1000)
        self.assertEqual([t for t, _ in res],
                         ["driverLaps:44", "driverLaps:1", "driverLaps:44"])  # offsets 100,150,200
        self.assertNotIn("driverLapClassification:44", [t for t, _ in res])   # sibling excluded

    def test_prefix_history_respects_offset_cutoff(self):
        res = self.db.get_topic_prefix_history("driverLaps:", 150)
        self.assertEqual([t for t, _ in res], ["driverLaps:44", "driverLaps:1"])   # <= 150

    def test_list_lap_telemetry_parses_and_ranges(self):
        laps = self.db.list_lap_telemetry(1000)
        self.assertEqual(laps, [("44", 1, len(json.dumps([1, 2, 3])))])
        self.assertEqual(self.db.list_lap_telemetry(120), [])   # before the telemetryLap row

    def test_offset_index_present(self):
        names = {r[1] for r in self.db._conn.execute("PRAGMA index_list('messages')")}
        self.assertIn("idx_msg_offset", names)


class GetStateAt(unittest.TestCase):
    """get_state_at rewrite (per-topic seeks) must match the old contract:
    latest row per topic at/before offset, with the excluded topics dropped."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = SessionDatabase(self.dir, db_path=self.dir / "s.db")
        self.db.open()
        self.db.save_messages([
            (100, "", "driverGap:44", json.dumps({"g": 1})),
            (200, "", "driverGap:44", json.dumps({"g": 2})),   # latest <= 250
            (300, "", "driverGap:44", json.dumps({"g": 3})),   # after 250 — excluded by offset
            (150, "", "trackStatus", json.dumps({"s": "green"})),
            (50, "", "position", json.dumps({"p": 1})),                 # excluded (exact)
            (60, "", "raceControlMessage", json.dumps({"m": "x"})),     # excluded (exact)
            (70, "", "telemetryLap:44:1", json.dumps([1, 2])),          # excluded (prefix)
            (80, "", "liveTelemetry:44", json.dumps({"x": 1})),         # excluded (prefix)
        ])

    def tearDown(self):
        self.db.close()

    def test_latest_per_topic_with_offset_cutoff_and_excludes(self):
        state = self.db.get_state_at(250)
        self.assertEqual(set(state.keys()), {"driverGap:44", "trackStatus"})
        self.assertEqual(state["driverGap:44"], {"data": {"g": 2}, "offset_ms": 200})
        self.assertEqual(state["trackStatus"], {"data": {"s": "green"}, "offset_ms": 150})

    def test_offset_before_any_row_is_empty(self):
        self.assertEqual(self.db.get_state_at(10), {})

    def test_excluded_topics_never_returned(self):
        state = self.db.get_state_at(1000)
        self.assertNotIn("position", state)
        self.assertNotIn("raceControlMessage", state)
        self.assertFalse(any(k.startswith(("telemetryLap:", "liveTelemetry:")) for k in state))


if __name__ == "__main__":
    unittest.main()
