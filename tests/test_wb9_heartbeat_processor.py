"""WB-9 batch 1 — HeartbeatProcessor: re-emits every raw Heartbeat as a
persisted `heartbeat` topic carrying the beat's own clock time, so the live
playback edge keeps advancing during quiet periods (see module docstring).
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.heartbeat_processor import HeartbeatProcessor


class HeartbeatProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = HeartbeatProcessor(bus, "Race")
        proc.subscribe()
        got = []
        bus.on("heartbeat", lambda d, ct: got.append((d, ct)))
        return bus, got

    def test_heartbeat_reemitted_with_clock_time_as_utc(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        bus.emit("Heartbeat", {"Utc": "2026-07-18T10:00:00Z"}, ct)
        self.assertEqual(len(got), 1)
        data, emitted_ct = got[0]
        self.assertEqual(data, {"utc": ct.isoformat()})
        self.assertEqual(emitted_ct, ct)

    def test_every_beat_emits_its_own_row_no_dedup(self):
        # Each beat's payload carries its own timestamp, so back-to-back
        # heartbeats must each produce a row (never de-duplicated) — that is
        # the entire point of this processor (keep MAX(offset_ms) advancing).
        bus, got = self._make()
        ct1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        ct2 = ct1 + timedelta(seconds=5)
        bus.emit("Heartbeat", {}, ct1)
        bus.emit("Heartbeat", {}, ct2)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0][0]["utc"], ct1.isoformat())
        self.assertEqual(got[1][0]["utc"], ct2.isoformat())
        self.assertNotEqual(got[0][0], got[1][0])


if __name__ == "__main__":
    unittest.main()
