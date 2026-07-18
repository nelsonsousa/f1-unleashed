"""B01 [P3] — the CLI login must persist f1auth.json owner-only (0600); it holds
the subscription JWT / raw login cookie.
"""
import json
import stat
import tempfile
import unittest
from pathlib import Path

from app.cli.login import _write_secure


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class AuthTokenPerms(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_new_file_is_0600(self):
        p = self.tmp / "f1auth.json"
        _write_secure(p, {"subscription_token": "x"})
        self.assertEqual(_mode(p), 0o600)

    def test_tightens_preexisting_loose_file(self):
        p = self.tmp / "loose.json"
        p.write_text("{}")
        p.chmod(0o644)
        _write_secure(p, {"raw_cookie": "y"})
        self.assertEqual(_mode(p), 0o600)

    def test_content_is_written(self):
        p = self.tmp / "c.json"
        _write_secure(p, {"subscription_token": "abc"})
        self.assertEqual(json.loads(p.read_text())["subscription_token"], "abc")


if __name__ == "__main__":
    unittest.main()
