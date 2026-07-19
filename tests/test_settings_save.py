"""B02 [P3] — settings.save() must not swallow a write failure. If the
settings.json write raises OSError (disk full / permissions), save() has to
propagate it so PUT /settings maps it to a 500 — instead of returning 200 with
"saved" settings that never hit disk. And the in-memory cache must reflect only
what was actually persisted (no phantom value that reverts on restart).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import settings as settings_store


class SettingsSave(unittest.IsolatedAsyncioTestCase):
    def test_write_failure_propagates(self):
        fake_file = mock.MagicMock()
        fake_file.read_text.side_effect = FileNotFoundError()   # load() -> defaults
        fake_file.write_text.side_effect = OSError("No space left on device")
        with mock.patch.object(settings_store, "SETTINGS_FILE", fake_file), \
             mock.patch.object(settings_store, "DATA_HOME", mock.MagicMock()), \
             mock.patch.object(settings_store, "_cache", None):
            # RIGHT: a failed persist raises, so the router returns 500 (not 200).
            with self.assertRaises(OSError):
                settings_store.save({"ntfy": {"topic": "spa-fp3"}})
            # RIGHT: nothing persisted -> cache must not carry the phantom value.
            self.assertNotEqual(
                settings_store.get("ntfy.topic"), "spa-fp3",
                "cache must not reflect an unpersisted change",
            )

    async def test_cache_location_reports_500_when_pointer_save_fails(self):
        # move succeeds/skipped but persisting the new cacheDir pointer fails ->
        # must be a 500 telling the user, not a silent 200 with a stale pointer.
        from fastapi import HTTPException
        from app import config
        from app.routers.settings import set_cache_location

        with tempfile.TemporaryDirectory() as old_d, tempfile.TemporaryDirectory() as new_d:
            with mock.patch.object(config, "CACHE_DIR", Path(old_d)), \
                 mock.patch.object(settings_store, "save",
                                   side_effect=OSError("No space left on device")):
                with self.assertRaises(HTTPException) as ctx:
                    await set_cache_location({"path": new_d, "move": False})
                self.assertEqual(ctx.exception.status_code, 500)

    def test_successful_save_persists_and_returns(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            sfile = home / "settings.json"
            with mock.patch.object(settings_store, "SETTINGS_FILE", sfile), \
                 mock.patch.object(settings_store, "DATA_HOME", home), \
                 mock.patch.object(settings_store, "_cache", None):
                out = settings_store.save({"ntfy": {"topic": "spa-fp3"}})
                self.assertEqual(out["ntfy"]["topic"], "spa-fp3")      # returned
                on_disk = json.loads(sfile.read_text())
                self.assertEqual(on_disk["ntfy"]["topic"], "spa-fp3")  # persisted


if __name__ == "__main__":
    unittest.main()
