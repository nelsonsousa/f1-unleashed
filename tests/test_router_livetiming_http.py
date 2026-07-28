"""WB-7 — HTTP-level contract tests for app/routers/livetiming.py.

livetiming_fetcher and session_manager are module singletons imported into
the router; patched there directly. Nothing here touches the real F1 CDN
or the local cache on disk.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
import app.routers.livetiming as livetiming_router
from app.services.livetiming_fetcher import Meeting, Session


class CachedSessionsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_lists_cached_sessions(self):
        fixture = [{
            "name": "2026_1287_Sakhir_11307_Race",
            "path": "/tmp/cache/2026/01_Bahrain/Race",
            "year": "2026", "meeting": "Bahrain", "location": "Sakhir",
            "session": "Race", "session_type": "Race", "session_key": "11307",
            "size_mb": 12.3, "modified": "2026-03-08T17:00:00",
            "has_jsonl": True, "has_audio": True,
            "data_status": "ok", "audio_status": "ok", "weather_status": "ok",
        }]
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_cached_sessions", return_value=fixture):
            r = self.client.get("/api/v1/livetiming/cached")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["name"], "2026_1287_Sakhir_11307_Race")

    def test_empty_cache_returns_empty_list(self):
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_cached_sessions", return_value=[]):
            r = self.client.get("/api/v1/livetiming/cached")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])


class FetchSessionEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _meeting_session(self):
        meeting = Meeting(key=1287, name="Bahrain", official_name="Bahrain Grand Prix",
                           location="Sakhir", country="Bahrain", circuit="BIC", number=1)
        session = Session(key=11307, name="Race", type="Race", path="Race",
                           start_date=None, end_date=None, gmt_offset="+03:00")
        return meeting, session

    def test_session_not_found_falls_through_to_500(self):
        # FINDING (not fixed here — test-only diff): fetch_session's inner
        # `raise HTTPException(404, ...)` has no `except HTTPException: raise`
        # guard before the generic `except Exception` handler (unlike e.g.
        # races.py's endpoints, which do have that guard), so the 404 is
        # caught and re-wrapped as a 500. Asserting the actual current
        # behavior here; see test-plan notes for the fix recommendation.
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_session_by_name", mock.AsyncMock(return_value=None)):
            r = self.client.post("/api/v1/livetiming/fetch", json={
                "year": 2026, "meeting_name": "Nowhere", "session_type": "Race",
            })
        self.assertEqual(r.status_code, 500)
        self.assertIn("Session not found", r.json()["detail"])

    def test_already_cached_short_circuits_fetch(self):
        meeting, session = self._meeting_session()
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_session_by_name", mock.AsyncMock(return_value=(meeting, session))), \
             mock.patch.object(livetiming_router.livetiming_fetcher,
                                "is_session_cached", return_value=True), \
             mock.patch.object(livetiming_router.livetiming_fetcher,
                                "_get_session_cache_dir", return_value="/tmp/cache/bahrain/race"), \
             mock.patch.object(livetiming_router.livetiming_fetcher,
                                "fetch_session") as fetch_mock:
            r = self.client.post("/api/v1/livetiming/fetch", json={
                "year": 2026, "meeting_name": "Bahrain", "session_type": "Race",
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["cache_path"], "/tmp/cache/bahrain/race")
        fetch_mock.assert_not_called()

    def test_fetch_failure_maps_to_400_on_value_error(self):
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_session_by_name",
                                mock.AsyncMock(side_effect=ValueError("bad session"))):
            r = self.client.post("/api/v1/livetiming/fetch", json={
                "year": 2026, "meeting_name": "Bahrain", "session_type": "Race",
            })
        self.assertEqual(r.status_code, 400)

    def test_fetch_request_requires_fields(self):
        r = self.client.post("/api/v1/livetiming/fetch", json={"year": 2026})
        self.assertEqual(r.status_code, 422)


class SessionInfoEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_session_not_found_is_404(self):
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "get_session_by_name", mock.AsyncMock(return_value=None)):
            r = self.client.get("/api/v1/livetiming/session/2026/Nowhere/Race/info")
        self.assertEqual(r.status_code, 404)


class AnalysisEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_pecking_order_unknown_session_is_404(self):
        with mock.patch.object(livetiming_router.session_manager,
                                "_find_session_path", return_value=None):
            r = self.client.get("/api/v1/livetiming/analysis/pecking_order/does-not-exist")
        self.assertEqual(r.status_code, 404)

    def test_pit_loss_estimate_unknown_session_is_404(self):
        with mock.patch.object(livetiming_router.session_manager,
                                "_find_session_path", return_value=None):
            r = self.client.get("/api/v1/livetiming/analysis/pit_loss_estimate/does-not-exist")
        self.assertEqual(r.status_code, 404)


class DeleteCachedSessionEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_delete_unknown_session_is_404(self):
        with mock.patch.object(livetiming_router.livetiming_fetcher,
                                "find_cached_session_path", return_value=None):
            r = self.client.delete("/api/v1/livetiming/cached/does-not-exist")
        self.assertEqual(r.status_code, 404)

    def test_delete_existing_session_removes_it(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            session_dir = Path(d) / "session"
            session_dir.mkdir()
            (session_dir / "live.jsonl").write_text("{}")
            with mock.patch.object(livetiming_router.livetiming_fetcher,
                                    "find_cached_session_path", return_value=session_dir):
                r = self.client.delete("/api/v1/livetiming/cached/session")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["success"])
            self.assertFalse(session_dir.exists())


if __name__ == "__main__":
    unittest.main()
