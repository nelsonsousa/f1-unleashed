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


if __name__ == "__main__":
    unittest.main()
