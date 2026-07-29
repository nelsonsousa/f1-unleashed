"""Regression (verification/fix-attempt-2, must-fix 2): the continuous dedup
rule (§9.1) must only apply to messages that survive `preprocessor.py`'s
SessionInfo.Key gate-buffer, not run upstream of it.

Pre-fix, `StreamNormalizer`'s dedup ran on every line as it was produced
(inside `process_line`), BEFORE `preprocessor.py`'s own gate-buffer logic
decided whether that line would ever actually be forwarded to the bus. A
duplicate-content baseline-state message (TrackStatus, SessionStatus,
WeatherData, LapCount, TopThree, ...) that arrived pre-gate and did NOT match
the gate-opening SessionInfo's timestamp (and wasn't DriverList) got buffered
and then silently discarded at gate-flush — but had already consumed the
normalizer's "last emitted" dedup state for that topic. The REAL subsequent
message with the same content was then wrongly suppressed as a duplicate,
even though nothing for that topic had actually reached the bus yet.

The fix: `preprocessor.py` disables the normalizer's dedup
(`set_dedup_enabled(False)`) for the whole pre-gate buffering window, and
re-enables it exactly when the gate opens — restoring the pre-redesign
property that dedup state is only ever updated by messages that are actually
going to be forwarded (previously true by construction, since dedup lived at
the OUTPUT layer).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


class PreGateDiscardedDuplicateDoesNotSuppressTheRealMessage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        self.si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": self.si}))

    def _write(self, lines: list[str]) -> None:
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_pre_gate_discarded_track_status_does_not_suppress_the_real_one(self):
        lines = [
            # Pre-gate TrackStatus, timestamp does NOT match the
            # gate-opening SessionInfo (below) and it isn't DriverList, so
            # it is buffered and then DISCARDED at gate-flush — never
            # actually reaches the bus.
            _env("TrackStatus", "2026-07-18T09:59:50.000Z", {"Status": "1", "Message": "AllClear"}),
            # Gate-opening SessionInfo.
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", self.si),
            # The REAL TrackStatus — same content as the discarded one
            # above — arriving after the gate has opened.
            _env("TrackStatus", "2026-07-18T10:00:02.000Z", {"Status": "1", "Message": "AllClear"}),
        ]
        self._write(lines)

        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")

        emitted_track_status = []
        real_emit = p._bus.emit

        def _spy_emit(topic, data, clock_time):
            if topic == "TrackStatus":
                emitted_track_status.append(data)
            return real_emit(topic, data, clock_time)

        p._bus.emit = _spy_emit

        try:
            await p.run()
            self.assertFalse(p.failed)

            # The real, post-gate TrackStatus must have reached the bus —
            # pre-fix, it was silently suppressed as a "duplicate" of the
            # pre-gate message that was itself discarded and never emitted.
            self.assertEqual(len(emitted_track_status), 1)
            self.assertEqual(emitted_track_status[0], {"Status": "1", "Message": "AllClear"})
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
