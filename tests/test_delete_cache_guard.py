"""B01 [P1] — DELETE /cached/{name:path} must never rmtree the cache root / a
season / an event dir. Guarded at the root cause: find_cached_session_path returns
only genuine session directories.
"""
import tempfile
import unittest
from pathlib import Path

from app.services.livetiming_fetcher import LiveTimingFetcher


class DeleteCacheGuard(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache"
        self.sess = self.cache / "2026" / "1290_Spa" / "11330_Q"
        self.sess.mkdir(parents=True)
        (self.sess / "subscribe.json").write_text("{}")
        # bypass __init__/config; force the legacy fallback (primary match returns nothing)
        self.f = LiveTimingFetcher.__new__(LiveTimingFetcher)
        self.f.cache_dir = self.cache
        self.f.get_cached_sessions = lambda: []

    def test_rejects_root_season_event_and_traversal(self):
        for bad in (".", "", "2026", "2026/1290_Spa", "../../../etc", "../.."):
            self.assertIsNone(self.f.find_cached_session_path(bad),
                              f"non-session path leaked for {bad!r}")

    def test_rejects_dir_without_session_files(self):
        (self.cache / "2026" / "notasession").mkdir(parents=True)
        self.assertIsNone(self.f.find_cached_session_path("2026/notasession"))

    def test_accepts_genuine_session(self):
        self.assertEqual(self.f.find_cached_session_path("2026/1290_Spa/11330_Q"),
                         self.sess.resolve())


if __name__ == "__main__":
    unittest.main()
