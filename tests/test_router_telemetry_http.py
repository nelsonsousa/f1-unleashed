"""WB-7 — HTTP-level contract tests for app/routers/telemetry.py."""
import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
import app.routers.telemetry as telemetry_router


class TelemetryStatusEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_status_reports_enabled_and_dir(self):
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=True), \
             mock.patch.object(telemetry_router.telemetry, "TELEMETRY_DIR", "/tmp/telemetry"):
            r = self.client.get("/api/v1/telemetry/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"enabled": True, "dir": "/tmp/telemetry"})

    def test_status_reports_disabled(self):
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=False), \
             mock.patch.object(telemetry_router.telemetry, "TELEMETRY_DIR", "/tmp/telemetry"):
            r = self.client.get("/api/v1/telemetry/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])


class AudioTimelineEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_post_rejected_when_telemetry_disabled(self):
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=False):
            r = self.client.post(
                "/api/v1/telemetry/audio-timeline",
                params={"session": "2026_Spa_Race"},
                content=json.dumps({"marks": []}),
            )
        self.assertEqual(r.status_code, 403)

    def test_post_saves_valid_timeline_when_enabled(self):
        from pathlib import Path
        fake_path = Path("/tmp/telemetry/2026_Spa_Race/client_timeline.json")
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=True), \
             mock.patch.object(telemetry_router.telemetry, "save_client_timeline",
                                return_value=fake_path) as m:
            r = self.client.post(
                "/api/v1/telemetry/audio-timeline",
                params={"session": "2026_Spa_Race"},
                content=json.dumps({"marks": [1, 2, 3]}),
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"saved": "client_timeline.json"})
        m.assert_called_once_with("2026_Spa_Race", {"marks": [1, 2, 3]})

    def test_post_rejects_non_json_body(self):
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=True):
            r = self.client.post(
                "/api/v1/telemetry/audio-timeline",
                params={"session": "2026_Spa_Race"},
                content=b"not json",
            )
        self.assertEqual(r.status_code, 400)

    def test_post_rejects_oversized_body(self):
        oversized = json.dumps({"pad": "x" * (8 * 1024 * 1024 + 1)}).encode()
        with mock.patch.object(telemetry_router.telemetry, "enabled", return_value=True):
            r = self.client.post(
                "/api/v1/telemetry/audio-timeline",
                params={"session": "2026_Spa_Race"},
                content=oversized,
            )
        self.assertEqual(r.status_code, 413)


if __name__ == "__main__":
    unittest.main()
