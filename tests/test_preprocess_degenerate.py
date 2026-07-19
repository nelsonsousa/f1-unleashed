"""B02 [P3] — SessionPreProcessor must build a degenerate session (only SessionInfo,
nothing after it) as an empty-but-valid build, not crash with an UnboundLocalError
(`filtered`) that then gets reported to the client as a build FAILURE.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


class PreprocessDegenerateFile(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        # folder name -> _extract_session_key == 11330 (the gate key)
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))
        # The ONLY message: the gate-opening SessionInfo, nothing after it.
        env = {"Type": "SessionInfo", "DateTime": "2026-07-18T10:00:00.000Z", "Json": si}
        (self.sess / "live.jsonl").write_text(json.dumps(env) + "\n")

    async def test_sessioninfo_only_builds_without_crashing(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")
            await p.run()   # RIGHT: completes; empty-but-valid, no UnboundLocalError
        self.assertFalse(p.failed, "degenerate file should build, not be marked failed")


if __name__ == "__main__":
    unittest.main()
