"""Telemetry sink (card VOPkIiAh): opt-in server-side event log + client-timeline
persistence, into a dedicated subfolder, new files only. Pins: name sanitising,
disabled = no-op (no disk writes), enabled appends jsonl with a wall-clock ms +
kind, and client-timeline saves a fresh file with the payload.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import telemetry


class TelemetrySink(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._p = mock.patch.object(telemetry, "TELEMETRY_DIR", self.dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_safe_sanitises(self):
        self.assertEqual(telemetry._safe("2026/1290 Spa!"), "2026_1290_Spa_")

    def test_record_is_noop_when_disabled(self):
        with mock.patch.object(telemetry.settings, "get", return_value=False):
            telemetry.record("s", "cap", {"a": 1})
        self.assertEqual(list(self.dir.iterdir()), [], "disabled → nothing written")

    def test_record_appends_jsonl_when_enabled(self):
        with mock.patch.object(telemetry.settings, "get", return_value=True):
            telemetry.record("2026_x", "cap", {"driver": "data", "edgeMs": 5})
            telemetry.record("2026_x", "cap", {"driver": "audio"})
        lines = (self.dir / "2026_x.server.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        r0 = json.loads(lines[0])
        self.assertEqual(r0["kind"], "cap")
        self.assertEqual(r0["driver"], "data")
        self.assertIsInstance(r0["t"], int)          # wall-clock ms stamp

    def test_save_client_timeline_writes_new_file(self):
        p = telemetry.save_client_timeline("2026_x", {"events": [1, 2, 3]})
        self.assertTrue(p.exists())
        self.assertTrue(p.name.startswith("2026_x.client."))
        self.assertTrue(p.name.endswith(".json"))
        self.assertEqual(json.loads(p.read_text())["events"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
