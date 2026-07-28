"""WB-9 batch 1 — TyreProcessor: current tyre + past-stint history per driver
from TimingAppData.Stints. Highest stint index = the running (current) tyre;
lower indices are finalised past stints.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.tyre_processor import TyreProcessor


class TyreProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = TyreProcessor(bus, "Race")
        proc.subscribe()
        current, history = [], []
        bus.on("currentTyre:44", lambda d, ct: current.append(d))
        bus.on("tyreHistory:44", lambda d, ct: history.append(d))
        return bus, current, history

    def test_single_stint_is_current_with_no_history(self):
        bus, current, history = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("TimingAppData", {"Lines": {"44": {"Stints": {
            "0": {"Compound": "SOFT", "New": "true", "TotalLaps": 3},
        }}}}, ct)
        self.assertEqual(current[-1], {"compound": "SOFT", "isNew": True, "age": 3})
        self.assertEqual(history[-1], [])

    def test_second_stint_moves_first_into_history(self):
        bus, current, history = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("TimingAppData", {"Lines": {"44": {"Stints": {
            "0": {"Compound": "SOFT", "New": "true", "TotalLaps": 15},
        }}}}, ct)
        bus.emit("TimingAppData", {"Lines": {"44": {"Stints": {
            "1": {"Compound": "MEDIUM", "New": "true", "TotalLaps": 1},
        }}}}, ct)
        self.assertEqual(current[-1], {"compound": "MEDIUM", "isNew": True, "age": 1})
        self.assertEqual(history[-1], [{"compound": "SOFT", "totalLaps": 15, "isNew": True}])

    def test_new_field_string_false_is_parsed_as_bool(self):
        bus, current, history = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("TimingAppData", {"Lines": {"44": {"Stints": {
            "0": {"Compound": "HARD", "New": "false", "TotalLaps": 25},
        }}}}, ct)
        self.assertIs(current[-1]["isNew"], False)

    def test_missing_lines_field_does_not_emit(self):
        bus, current, history = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("TimingAppData", {}, ct)
        self.assertEqual(current, [])
        self.assertEqual(history, [])

    def test_stint_with_no_compound_yet_is_not_emitted(self):
        bus, current, history = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        # A stint entry that only carries New/TotalLaps (no Compound yet) is
        # still "changed" (updates internal state) but has nothing real to emit.
        bus.emit("TimingAppData", {"Lines": {"44": {"Stints": {
            "0": {"New": "true"},
        }}}}, ct)
        self.assertEqual(current, [])
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
