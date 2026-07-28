"""WB-9 batch 1 — ClockProcessor: emits `clock` {utc, sessionTime,
clockStatus} on every ExtrapolatedClock message, carrying forward whichever
fields aren't present in a partial update.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.clock_processor import ClockProcessor


class ClockProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = ClockProcessor(bus, "Race")
        proc.subscribe()
        got = []
        bus.on("clock", lambda d, ct: got.append(dict(d)))
        return bus, got

    def test_full_clock_message_emits_all_fields(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("ExtrapolatedClock", {
            "Utc": "2026-07-18T10:00:00.000Z",
            "Remaining": "01:00:00",
            "Extrapolating": True,
        }, ct)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], {
            "utc": "2026-07-18T10:00:00.000Z",
            "sessionTime": "01:00:00",
            "clockStatus": "play",
        })

    def test_extrapolating_false_reports_pause(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("ExtrapolatedClock", {"Extrapolating": False}, ct)
        self.assertEqual(got[-1]["clockStatus"], "pause")

    def test_partial_update_carries_forward_previous_fields(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("ExtrapolatedClock", {
            "Utc": "2026-07-18T10:00:00.000Z",
            "Remaining": "01:00:00",
            "Extrapolating": True,
        }, ct)
        # Only Remaining changes on the next tick — Utc/clockStatus carry forward.
        bus.emit("ExtrapolatedClock", {"Remaining": "00:59:59"}, ct)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["utc"], "2026-07-18T10:00:00.000Z")
        self.assertEqual(got[1]["sessionTime"], "00:59:59")
        self.assertEqual(got[1]["clockStatus"], "play")

    def test_non_dict_clock_payload_does_not_emit_or_crash(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("ExtrapolatedClock", None, ct)
        self.assertEqual(got, [])

    def test_session_info_with_malformed_gmt_offset_does_not_crash(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionInfo", {"GmtOffset": "not-a-time"}, ct)
        # Malformed offset is swallowed; a subsequent clock tick still works.
        bus.emit("ExtrapolatedClock", {"Utc": "x", "Remaining": "y",
                                        "Extrapolating": True}, ct)
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
