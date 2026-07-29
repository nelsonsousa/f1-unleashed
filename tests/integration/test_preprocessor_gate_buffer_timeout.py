"""Coverage (critical path, must-fix 3): the pre-gate buffer's 60-second
timeout-discard branch in `preprocessor.py` (`GATE_TIMEOUT_S`) — specifically
the `self._gate_first_ts = msg.utc_timestamp` reset that runs when a message
arrives more than `GATE_TIMEOUT_S` after the first buffered message with no
matching SessionInfo in between, discarding the stale buffer and restarting
the timeout window from the message that triggered the discard.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


class GateBufferTimeoutDiscard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

        lines = [
            # First pre-gate message — starts the 60s timeout window.
            _env("Heartbeat", "2026-07-18T09:00:00.000Z", {"n": 1}),
            # Arrives 70s later, with no matching SessionInfo in between —
            # triggers the timeout-discard branch: the buffer (currently
            # holding just the first Heartbeat) is cleared and the timeout
            # window restarts from THIS message.
            _env("Heartbeat", "2026-07-18T09:01:10.000Z", {"n": 2}),
            # Gate-opening SessionInfo, well within 60s of the restarted
            # window — proves the pipeline recovers normally after a
            # timeout-discard rather than getting stuck.
            _env("SessionInfo", "2026-07-18T09:01:15.000Z", si),
            _env("Heartbeat", "2026-07-18T09:01:20.000Z", {"n": 3}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_stale_buffer_discarded_and_timeout_window_restarts(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")
        try:
            with self.assertLogs("app.processing.preprocessor", level="WARNING") as log:
                await p.run()
            self.assertFalse(p.failed)
            self.assertTrue(any("Gate timeout" in m for m in log.output))

            # The gate still opened normally (SessionInfo matched) after the
            # timeout-discard — the pipeline is not stuck.
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T09:01:15+00:00")

            # Post-gate Heartbeat (n=3) was processed normally.
            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
