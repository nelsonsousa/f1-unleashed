"""WB-9 batch 1 — TeamRadioProcessor: the initial subscribe backlog (a LIST of
pre-session clips) is seen but never played; incremental live clips (a DICT,
one at a time) are emitted for playback at their broadcast Utc, deduped by
Path.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.team_radio_processor import TeamRadioProcessor


class TeamRadioProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = TeamRadioProcessor(bus, "Race")
        proc.subscribe()
        got = []
        bus.on("teamRadio", lambda d, ct: got.append((d, ct)))
        return bus, got

    def test_initial_backlog_list_is_not_played(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
        bus.emit("TeamRadio", {"Captures": [
            {"Path": "team_radio/44_1.mp3", "RacingNumber": "44",
             "Utc": "2026-07-18T08:00:00.000Z"},
        ]}, ct)
        self.assertEqual(got, [], "pre-session backlog must not be queued for playback")

    def test_incremental_live_clip_is_emitted_at_its_utc(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        clip_utc = "2026-07-18T10:05:30.000Z"
        bus.emit("TeamRadio", {"Captures": {
            "0": {"Path": "team_radio/44_2.mp3", "RacingNumber": "44", "Utc": clip_utc},
        }}, ct)
        self.assertEqual(len(got), 1)
        data, emitted_ct = got[0]
        self.assertEqual(data["num"], "44")
        self.assertEqual(data["file"], "44_2.mp3")
        self.assertEqual(emitted_ct, datetime.fromisoformat(clip_utc.replace("Z", "+00:00")))

    def test_duplicate_path_is_deduped(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        clip = {"Path": "team_radio/44_3.mp3", "RacingNumber": "44",
                "Utc": "2026-07-18T10:10:00.000Z"}
        bus.emit("TeamRadio", {"Captures": {"0": clip}}, ct)
        bus.emit("TeamRadio", {"Captures": {"0": clip}}, ct)   # same path again
        self.assertEqual(len(got), 1, "repeated Path must not be replayed")

    def test_missing_captures_field_is_ignored(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("TeamRadio", {}, ct)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
