"""WB-9 batch 2 — ChampionshipProcessor: races/sprints only, merges sticky
ChampionshipPrediction deltas per driver/team, joins in driver identity from
driverList, and emits sorted championshipDrivers/championshipConstructors
(see module docstring for the pointsGained/positionsGained formulas).
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.championship_processor import ChampionshipProcessor


class ChampionshipProcessorTests(unittest.TestCase):
    def _make(self, session_type="race"):
        bus = SessionMessageBus()
        proc = ChampionshipProcessor(bus, session_type)
        proc.subscribe()
        drivers = []
        constructors = []
        bus.on("championshipDrivers", lambda d, ct: drivers.append(d))
        bus.on("championshipConstructors", lambda d, ct: constructors.append(d))
        return bus, drivers, constructors

    def _ct(self):
        return datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)

    def test_inactive_outside_race_and_sprint_never_subscribes(self):
        bus, drivers, constructors = self._make(session_type="Qualifying")
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"CurrentPosition": 2, "PredictedPosition": 1,
                                "CurrentPoints": 100, "PredictedPoints": 125}},
        }, self._ct())
        self.assertEqual(drivers, [])
        self.assertEqual(constructors, [])

    def test_active_for_race_lowercase_session_type(self):
        bus, drivers, _ = self._make(session_type="race")
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"CurrentPosition": 2, "PredictedPosition": 1,
                                "CurrentPoints": 100, "PredictedPoints": 125}},
        }, self._ct())
        self.assertEqual(len(drivers), 1)

    def test_active_for_sprint(self):
        bus, drivers, _ = self._make(session_type="sprint")
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"CurrentPosition": 2, "PredictedPosition": 1,
                                "CurrentPoints": 100, "PredictedPoints": 125}},
        }, self._ct())
        self.assertEqual(len(drivers), 1)

    def test_points_and_positions_gained_computed_from_prediction(self):
        bus, drivers, _ = self._make()
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"CurrentPosition": 3, "PredictedPosition": 1,
                                "CurrentPoints": 100, "PredictedPoints": 125}},
        }, self._ct())
        d = drivers[-1][0]
        self.assertEqual(d["driverNumber"], "44")
        self.assertEqual(d["predictedPosition"], 1)
        self.assertEqual(d["predictedPoints"], 125)
        self.assertEqual(d["pointsGained"], 25)
        self.assertEqual(d["positionsGained"], 2)  # climbed from 3rd to 1st

    def test_drivers_sorted_by_predicted_position_nones_last(self):
        bus, drivers, _ = self._make()
        bus.emit("ChampionshipPrediction", {
            "Drivers": {
                "44": {"PredictedPosition": 3},
                "1": {"PredictedPosition": 1},
                "16": {},  # no PredictedPosition -> sorts last
            },
        }, self._ct())
        order = [d["driverNumber"] for d in drivers[-1]]
        self.assertEqual(order, ["1", "44", "16"])

    def test_driver_list_arrival_fills_name_and_colour_and_reemits(self):
        bus, drivers, _ = self._make()
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"CurrentPosition": 2, "PredictedPosition": 1,
                                "CurrentPoints": 100, "PredictedPoints": 110}},
        }, self._ct())
        self.assertEqual(len(drivers), 1)
        self.assertIsNone(drivers[-1][0]["teamColour"])
        self.assertEqual(drivers[-1][0]["driverName"], "44")  # fallback to num

        bus.emit("driverList", {
            "44": {"lastName": "Hamilton", "color": "#27F4D2", "teamName": "Mercedes"},
        }, self._ct())
        self.assertEqual(len(drivers), 2, "driverList arrival must re-emit")
        self.assertEqual(drivers[-1][0]["driverName"], "Hamilton")
        self.assertEqual(drivers[-1][0]["teamColour"], "#27F4D2")

    def test_driver_list_before_any_prediction_does_not_emit(self):
        bus, drivers, _ = self._make()
        bus.emit("driverList", {
            "44": {"lastName": "Hamilton", "color": "#27F4D2", "teamName": "Mercedes"},
        }, self._ct())
        self.assertEqual(drivers, [])

    def test_constructors_sticky_team_name_only_carried_once(self):
        bus, _, constructors = self._make()
        bus.emit("ChampionshipPrediction", {
            "Teams": {
                "McLaren Mercedes": {"TeamName": "McLaren", "CurrentPosition": 1,
                                      "PredictedPosition": 1, "CurrentPoints": 400,
                                      "PredictedPoints": 420},
            },
        }, self._ct())
        self.assertEqual(constructors[-1][0]["teamName"], "McLaren")

        # Follow-up message carries no TeamName (F1's sticky-delta shape) —
        # must still resolve to the previously-seen short name, not fork
        # a duplicate keyed by the raw F1 key.
        bus.emit("ChampionshipPrediction", {
            "Teams": {"McLaren Mercedes": {"PredictedPoints": 425}},
        }, self._ct())
        self.assertEqual(len(constructors[-1]), 1)
        self.assertEqual(constructors[-1][0]["teamName"], "McLaren")
        self.assertEqual(constructors[-1][0]["predictedPoints"], 425)

    def test_unchanged_prediction_does_not_reemit(self):
        bus, drivers, _ = self._make()
        entry = {"CurrentPosition": 2, "PredictedPosition": 1,
                  "CurrentPoints": 100, "PredictedPoints": 110}
        bus.emit("ChampionshipPrediction", {"Drivers": {"44": dict(entry)}}, self._ct())
        self.assertEqual(len(drivers), 1)
        bus.emit("ChampionshipPrediction", {"Drivers": {"44": dict(entry)}}, self._ct())
        self.assertEqual(len(drivers), 1, "identical fields must not trigger a re-emit")

    def test_non_dict_payload_is_ignored(self):
        bus, drivers, constructors = self._make()
        bus.emit("ChampionshipPrediction", "oops", self._ct())
        self.assertEqual(drivers, [])
        self.assertEqual(constructors, [])

    def test_missing_points_or_position_fields_yield_none_gains(self):
        bus, drivers, _ = self._make()
        bus.emit("ChampionshipPrediction", {
            "Drivers": {"44": {"PredictedPosition": 1}},  # no points at all
        }, self._ct())
        d = drivers[-1][0]
        self.assertIsNone(d["pointsGained"])
        self.assertIsNone(d["positionsGained"])


if __name__ == "__main__":
    unittest.main()
