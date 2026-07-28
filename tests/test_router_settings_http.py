"""WB-7 — HTTP-level contract tests for app/routers/settings.py.

settings_store and config are module singletons imported into the router;
patched there directly (matches tests/test_settings_save.py's own style for
this module, just exercised through TestClient instead of calling the
handler function directly).
"""
import unittest
from unittest import mock
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
import app.routers.settings as settings_router


class GetSettingsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_get_settings_includes_resolved_cache_paths(self):
        with mock.patch.object(settings_router.settings_store, "load",
                                return_value={"ntfy": {"topic": "spa-fp3"}}), \
             mock.patch.object(settings_router.config, "CACHE_DIR", Path("/tmp/cache")), \
             mock.patch.object(settings_router.settings_store, "DATA_HOME", Path("/tmp/home")):
            r = self.client.get("/api/v1/settings")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["ntfy"]["topic"], "spa-fp3")
        self.assertEqual(body["_cacheDir"], "/tmp/cache")
        self.assertEqual(body["_dataHome"], "/tmp/home")


class UpdateSettingsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_update_settings_persists_and_returns_merged(self):
        with mock.patch.object(settings_router.settings_store, "save",
                                return_value={"ntfy": {"topic": "new-topic"}}) as m:
            r = self.client.put("/api/v1/settings", json={"ntfy": {"topic": "new-topic"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ntfy"]["topic"], "new-topic")
        m.assert_called_once_with({"ntfy": {"topic": "new-topic"}})

    def test_update_settings_strips_cache_dir_before_saving(self):
        # cacheDir is managed only via /settings/cache-location — a generic PUT
        # must not be able to silently repoint the cache.
        with mock.patch.object(settings_router.settings_store, "save",
                                return_value={}) as m:
            self.client.put("/api/v1/settings", json={"cacheDir": "/evil/path", "ntfy": {}})
        m.assert_called_once_with({"ntfy": {}})

    def test_update_settings_rejects_non_object_body(self):
        # FastAPI validates the `dict[str, Any]` body type before the handler's
        # own isinstance() guard ever runs, so a JSON array is rejected at the
        # request-validation layer (422), not inside update_settings() (400).
        r = self.client.put("/api/v1/settings", json=["not", "an", "object"])
        self.assertEqual(r.status_code, 422)

    def test_update_settings_persist_failure_is_500(self):
        with mock.patch.object(settings_router.settings_store, "save",
                                side_effect=OSError("No space left on device")):
            r = self.client.put("/api/v1/settings", json={"ntfy": {"topic": "x"}})
        self.assertEqual(r.status_code, 500)


class CacheLocationEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_missing_path_is_400(self):
        r = self.client.post("/api/v1/settings/cache-location", json={"move": False})
        self.assertEqual(r.status_code, 400)

    def test_blank_path_is_400(self):
        r = self.client.post("/api/v1/settings/cache-location", json={"path": "   "})
        self.assertEqual(r.status_code, 400)

    def test_overlapping_target_is_rejected(self):
        with mock.patch.object(settings_router.config, "CACHE_DIR", Path("/tmp/cache")):
            r = self.client.post(
                "/api/v1/settings/cache-location",
                json={"path": "/tmp/cache/nested", "move": False},
            )
        self.assertEqual(r.status_code, 400)
        self.assertIn("overlap", r.json()["detail"])

    def test_successful_pointer_update_without_move(self):
        with mock.patch.object(settings_router.config, "CACHE_DIR", Path("/tmp/old-cache")), \
             mock.patch.object(settings_router.settings_store, "save", return_value={}) as m:
            r = self.client.post(
                "/api/v1/settings/cache-location",
                json={"path": "/tmp/new-cache", "move": False},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cacheDir"], "/tmp/new-cache")
        self.assertTrue(body["restartRequired"])
        m.assert_called_once_with({"cacheDir": "/tmp/new-cache"})


if __name__ == "__main__":
    unittest.main()
