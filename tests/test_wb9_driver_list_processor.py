"""WB-9 batch 1 — DriverListProcessor: merges incremental DriverList updates
per car number (a later message may fill in fields an earlier one omitted)
and resolves a display colour (F1 TeamColour if present, else the hardcoded
fallback).
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.driver_list_processor import DriverListProcessor
from app.processing.processors.standings_processor import TEAM_COLORS, DEFAULT_CAR_COLOR


class DriverListProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = DriverListProcessor(bus, "Race")
        proc.subscribe()
        got = []
        bus.on("driverList", lambda d, ct: got.append(d))
        return bus, got

    def test_first_message_always_emits_even_with_no_new_info(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("DriverList", {"44": {}}, ct)
        self.assertEqual(len(got), 1, "the first DriverList must always emit")

    def test_team_colour_present_is_used_verbatim(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("DriverList", {
            "44": {"Tla": "HAM", "TeamName": "Mercedes", "TeamColour": "27F4D2"},
        }, ct)
        self.assertEqual(got[-1]["44"]["color"], "#27F4D2")
        self.assertEqual(got[-1]["44"]["tla"], "HAM")

    def test_missing_team_colour_falls_back_to_hardcoded_table(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("DriverList", {"1": {"Tla": "VER"}}, ct)
        expected = TEAM_COLORS.get("1", DEFAULT_CAR_COLOR)
        self.assertEqual(got[-1]["1"]["color"], expected)

    def test_later_message_merges_fields_missing_from_the_first(self):
        # Canonical case (Canada FP1 2026): first DriverList carries only
        # Tla/Line; TeamName/TeamColour arrive in a later incremental update.
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("DriverList", {"44": {"Tla": "HAM"}}, ct)
        bus.emit("DriverList", {"44": {"TeamName": "Mercedes", "TeamColour": "27F4D2"}}, ct)
        self.assertEqual(len(got), 2, "the merged update must trigger a re-emit")
        merged = got[-1]["44"]
        self.assertEqual(merged["tla"], "HAM")
        self.assertEqual(merged["teamName"], "Mercedes")
        self.assertEqual(merged["color"], "#27F4D2")

    def test_non_dict_payload_is_ignored(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("DriverList", "oops", ct)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
