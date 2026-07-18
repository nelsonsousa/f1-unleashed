"""B01 [P3] — clip_dest must keep every team-radio download under the session
cache dir; reject feed-supplied '..' paths (write-side traversal).
"""
import tempfile
import unittest
from pathlib import Path

from app.services.team_radio import clip_dest


class TeamRadioClipPath(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache"
        self.cache.mkdir(parents=True)

    def test_contains_legit_paths(self):
        d = clip_dest(self.cache, "TeamRadio/a.mp3")
        self.assertIsNotNone(d)
        self.assertTrue(d.is_relative_to(self.cache.resolve()))
        self.assertIsNotNone(clip_dest(self.cache, "/TeamRadio/b.mp3"))

    def test_rejects_traversal(self):
        for bad in ("../../../etc/passwd",
                    "TeamRadio/../../../../x",
                    "../../Users/x/.zshenv"):
            self.assertIsNone(clip_dest(self.cache, bad),
                              f"traversal path leaked for {bad!r}")


if __name__ == "__main__":
    unittest.main()
