"""WB-9 batch 2 — RaceControlProcessor: turns raw RaceControlMessages into
per-message `raceControlMessage` rows (server-computed colour) plus the
running `yellowFlag` sector-set topic, de-duplicating already-seen entries
by index/position so a re-sent accumulating snapshot doesn't reprocess
history (see module docstring).
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.race_control_processor import RaceControlProcessor


class RaceControlProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = RaceControlProcessor(bus, "Race")
        proc.subscribe()
        messages = []
        yellow = []
        bus.on("raceControlMessage", lambda d, ct: messages.append(d))
        bus.on("yellowFlag", lambda d, ct: yellow.append(d))
        return bus, messages, yellow

    def _ct(self, sec=0):
        return datetime(2026, 7, 18, 10, 0, sec, tzinfo=timezone.utc)

    def test_flag_category_colour_is_mapped_from_flag_field(self):
        bus, messages, _ = self._make()
        cases = [
            ("RED", "red"),
            ("YELLOW", "yellow"),
            ("DOUBLE YELLOW", "yellow"),
            ("GREEN", "green"),
            ("CLEAR", "green"),
            ("CHEQUERED", "chequered"),
            ("BLUE", "blue"),
            ("BLACK AND WHITE", "blackAndWhite"),
            ("PURPLE", "purple"),  # unmapped -> lowercased passthrough
        ]
        for i, (flag, expected_colour) in enumerate(cases):
            bus.emit("RaceControlMessages", {"Messages": {
                str(i): {"Category": "Flag", "Flag": flag, "Message": "m",
                         "Utc": "2026-07-18T10:00:00Z"},
            }}, self._ct())
        self.assertEqual(len(messages), len(cases))
        for (flag, expected_colour), msg in zip(cases, messages):
            self.assertEqual(msg["color"], expected_colour, flag)

    def test_safetycar_category_is_always_yellow(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "SafetyCar", "Message": "SAFETY CAR DEPLOYED"},
        }}, self._ct())
        self.assertEqual(messages[-1]["color"], "yellow")

    def test_other_category_under_investigation_is_yellow(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Other",
                  "Message": "CAR 44 (HAM) UNDER INVESTIGATION"},
        }}, self._ct())
        self.assertEqual(messages[-1]["color"], "yellow")

    def test_other_category_awarded_penalty_is_orange(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Other",
                  "Message": "CAR 44 (HAM) 5 SECOND TIME PENALTY"},
        }}, self._ct())
        self.assertEqual(messages[-1]["color"], "orange")

    def test_other_category_penalty_served_is_clear_not_orange(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Other",
                  "Message": "CAR 44 (HAM) 5 SECOND PENALTY SERVED"},
        }}, self._ct())
        self.assertEqual(messages[-1]["color"], "clear")

    def test_other_category_unrelated_text_is_clear(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Other", "Message": "TRACK SURFACE DRY"},
        }}, self._ct())
        self.assertEqual(messages[-1]["color"], "clear")

    def test_sector_yellow_flag_added_then_cleared(self):
        bus, _, yellow = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector",
                  "Sector": "3", "Message": "YELLOW IN SECTOR 3"},
        }}, self._ct())
        self.assertEqual(yellow[-1], [3])

        bus.emit("RaceControlMessages", {"Messages": {
            "1": {"Category": "Flag", "Flag": "CLEAR", "Scope": "Sector",
                  "Sector": "3", "Message": "CLEAR IN SECTOR 3"},
        }}, self._ct())
        self.assertEqual(yellow[-1], [])

    def test_track_scope_clear_wipes_all_yellow_sectors(self):
        bus, _, yellow = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector",
                  "Sector": "3", "Message": "YELLOW IN SECTOR 3"},
            "1": {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector",
                  "Sector": "5", "Message": "YELLOW IN SECTOR 5"},
        }}, self._ct())
        self.assertEqual(sorted(yellow[-1]), [3, 5])

        bus.emit("RaceControlMessages", {"Messages": {
            "2": {"Category": "Flag", "Flag": "CLEAR", "Scope": "Track",
                  "Message": "TRACK CLEAR"},
        }}, self._ct())
        self.assertEqual(yellow[-1], [])

    def test_track_scope_flag_does_not_emit_yellow_flag_topic(self):
        # Only SECTOR scope (and TRACK clear) touch yellowFlag; a non-clear
        # TRACK-scope flag (e.g. red) must not emit a yellowFlag update.
        bus, _, yellow = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Flag", "Flag": "RED", "Scope": "Track",
                  "Message": "RED FLAG"},
        }}, self._ct())
        self.assertEqual(yellow, [])

    def test_dict_indexed_messages_dedup_by_index_out_of_order(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": {
            "1": {"Category": "Other", "Message": "SECOND"},
            "0": {"Category": "Other", "Message": "FIRST"},
        }}, self._ct())
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["message"], "FIRST")
        self.assertEqual(messages[1]["message"], "SECOND")

        # Re-sending the same accumulated snapshot must not reprocess.
        bus.emit("RaceControlMessages", {"Messages": {
            "0": {"Category": "Other", "Message": "FIRST"},
            "1": {"Category": "Other", "Message": "SECOND"},
        }}, self._ct())
        self.assertEqual(len(messages), 2, "already-seen indices must be skipped")

    def test_list_messages_dedup_by_position(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": ["FIRST", {"Category": "Other", "Message": "SECOND"}]}, self._ct())
        # First entry is a string, not a dict -> ignored; second is processed.
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message"], "SECOND")

        # Re-emitting the same list (same length/positions) reprocesses nothing new.
        bus.emit("RaceControlMessages", {"Messages": ["FIRST", {"Category": "Other", "Message": "SECOND"}]}, self._ct())
        self.assertEqual(len(messages), 1)

    def test_messages_key_absent_falls_back_to_top_level_dict(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {
            "0": {"Category": "Other", "Message": "TOP LEVEL"},
        }, self._ct())
        self.assertEqual(messages[-1]["message"], "TOP LEVEL")

    def test_non_dict_payload_is_ignored(self):
        bus, messages, yellow = self._make()
        bus.emit("RaceControlMessages", "oops", self._ct())
        self.assertEqual(messages, [])
        self.assertEqual(yellow, [])

    def test_messages_value_wrong_type_is_ignored(self):
        bus, messages, _ = self._make()
        bus.emit("RaceControlMessages", {"Messages": "oops"}, self._ct())
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
