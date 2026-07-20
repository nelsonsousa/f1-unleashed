"""Instance override (multi-instance support): a gitignored instance.env lets a
checkout run as a second server with its own PORT + data home. Pins: file
parsing (comments/blanks/quotes), env-var-wins precedence, port coercion, and
data-home redirection with the OS default preserved when unset.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import settings as st


class ParseInstanceFile(unittest.TestCase):
    BODY = (
        "# a comment\n"
        "\n"
        "PORT=1951\n"
        '  F1_DATA_HOME = "/tmp/f1b"  \n'
        "EMPTY=\n"
    )

    def test_reads_key(self):
        self.assertEqual(st._parse_instance_file(self.BODY, "PORT"), "1951")

    def test_strips_whitespace_and_quotes(self):
        self.assertEqual(st._parse_instance_file(self.BODY, "F1_DATA_HOME"), "/tmp/f1b")

    def test_comment_blank_and_empty_value_are_none(self):
        self.assertIsNone(st._parse_instance_file(self.BODY, "EMPTY"))
        self.assertIsNone(st._parse_instance_file(self.BODY, "MISSING"))


class InstanceOverride(unittest.TestCase):
    def setUp(self):
        # isolate from any real env var + real instance.env in the repo
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("PORT", "F1_DATA_HOME"):
            os.environ.pop(k, None)
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "instance.env"
        self._patch = mock.patch.object(st, "_INSTANCE_FILE", self.file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()

    def test_env_var_wins_over_file(self):
        self.file.write_text("PORT=1951\n")
        os.environ["PORT"] = "1960"
        self.assertEqual(st._instance_override("PORT"), "1960")

    def test_falls_back_to_file(self):
        self.file.write_text("PORT=1951\n")
        self.assertEqual(st._instance_override("PORT"), "1951")

    def test_missing_file_is_none(self):
        self.assertIsNone(st._instance_override("PORT"))   # no file written

    def test_instance_port_coerces_and_defaults(self):
        self.file.write_text("PORT=1951\n")
        self.assertEqual(st.instance_port(), 1951)
        self.file.write_text("PORT=notanumber\n")
        self.assertEqual(st.instance_port(), 1950)          # bad value → default
        self.file.unlink()
        self.assertEqual(st.instance_port(default=1950), 1950)

    def test_data_home_override_redirects(self):
        self.file.write_text("F1_DATA_HOME=/tmp/f1-instance-b\n")
        self.assertEqual(st._os_data_home(), Path("/tmp/f1-instance-b"))

    def test_data_home_default_when_unset(self):
        # no override → some real OS path under the home dir, not our temp
        self.assertNotEqual(st._os_data_home(), self.tmp)
        self.assertTrue(str(st._os_data_home()))


if __name__ == "__main__":
    unittest.main()
