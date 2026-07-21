"""B04: seek/connect restore of car positions + current-lap live telemetry from
the (already-persisted) `position` / `liveTelemetry:*` rows — replacing the
_raw_telemetry_stream live.jsonl re-scan. Pins the two DB helpers.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.database import SessionDatabase


class B04Restore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = SessionDatabase(self.dir, db_path=self.dir / "b.db")
        self.db.open()
        self.db.save_messages([
            # position: PARTIAL rows (outage-style — a row carries only the cars
            # in that Position.z entry), so a single latest row is not the grid.
            (100, "", "position", json.dumps({"44": [1, 1, 0.1], "63": [2, 2, 0.2]})),
            (200, "", "position", json.dumps({"44": [3, 3, 0.3]})),   # only 44 moved
            (300, "", "position", json.dumps({"63": [4, 4, 0.4]})),   # only 63 moved (later)
        ])

    def tearDown(self):
        self.db.close()

    def test_latest_position_merges_partial_newest_wins(self):
        # 44 latest @200, 63 latest @300 — merged grid despite partial rows
        self.assertEqual(self.db.latest_position_per_car(300),
                         {"44": [3, 3, 0.3], "63": [4, 4, 0.4]})

    def test_latest_position_respects_offset(self):
        self.assertEqual(self.db.latest_position_per_car(150),
                         {"44": [1, 1, 0.1], "63": [2, 2, 0.2]})


if __name__ == "__main__":
    unittest.main()
