"""WB-9 batch 1 — TrackStatusProcessor: combines SessionStatus (authoritative)
with the safety-car overlay from TrackStatus/RaceControlMessages into a single
badge + scrubber marker, fired once per colour change.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.track_status_processor import TrackStatusProcessor


class TrackStatusProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = TrackStatusProcessor(bus, "Race")
        proc.subscribe()
        status, events = [], []
        bus.on("trackStatus", lambda d, ct: status.append(dict(d)))
        bus.on("event", lambda d, ct: events.append(d))
        return bus, status, events

    def test_session_started_emits_green_and_marker(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        self.assertEqual(status[-1], {"status": "green", "message": "GREEN FLAG"})
        self.assertEqual(events, ["GREEN"])

    def test_safety_car_overlay_applies_while_racing(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        bus.emit("TrackStatus", {"Message": "SCDeployed"}, ct)
        self.assertEqual(status[-1], {"status": "sc", "message": "SC DEPLOYED"})
        self.assertEqual(events, ["GREEN", "SC"])

    def test_all_clear_lifts_caution_back_to_green_no_duplicate_yellow_marker(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        bus.emit("TrackStatus", {"Message": "SCDeployed"}, ct)
        bus.emit("TrackStatus", {"Message": "VSCDeployed"}, ct)  # consecutive yellow
        bus.emit("TrackStatus", {"Message": "AllClear"}, ct)
        self.assertEqual(status[-1], {"status": "green", "message": "GREEN FLAG"})
        # SC->VSC is a consecutive yellow: only one "SC" marker, no extra "VSC".
        self.assertEqual(events, ["GREEN", "SC", "GREEN"])

    def test_red_flag_is_authoritative_and_ignores_safety_car_overlay(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        bus.emit("SessionStatus", {"Status": "Aborted"}, ct)
        bus.emit("TrackStatus", {"Message": "SCDeployed"}, ct)   # ignored: not racing
        self.assertEqual(status[-1], {"status": "red", "message": "RED FLAG"})

    def test_rcm_safety_car_deployed_sets_sc_state(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        bus.emit("RaceControlMessages", {"Messages": {"0": {
            "Category": "SafetyCar", "Mode": "SAFETY CAR", "Status": "DEPLOYED",
        }}}, ct)
        self.assertEqual(status[-1], {"status": "sc", "message": "SC DEPLOYED"})

    def test_unrecognised_status_is_ignored(self):
        bus, status, events = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "SomethingWeird"}, ct)
        self.assertEqual(status, [])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
