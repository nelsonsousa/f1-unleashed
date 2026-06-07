"""
Championship Prediction Processor.

Subscribes to: ChampionshipPrediction
Emits: display:championshipPrediction

Output (championshipPrediction), sorted by predicted position:
  drivers:      [{num, currentPoints, predictedPoints, pointsToday,
                  currentPosition, predictedPosition, positionChange}, ...]
  constructors: [{team, currentPoints, predictedPoints, pointsToday,
                  currentPosition, predictedPosition, positionChange}, ...]
pointsToday = predicted − current (points expected to be won today).
positionChange = current − predicted position (+N = climbed N places).
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class ChampionshipProcessor(Processor):
    """Accumulates championship prediction state and emits sorted arrays."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        # num -> {currentPosition, currentPoints, predictedPosition, predictedPoints}
        self._drivers: dict[str, dict] = {}
        # teamKey -> {teamName, currentPosition, currentPoints, predictedPosition, predictedPoints}
        self._teams: dict[str, dict] = {}

    def subscribe(self) -> None:
        self._bus.on("ChampionshipPrediction", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        changed = False

        drivers = data.get("Drivers")
        if isinstance(drivers, dict):
            for num, info in drivers.items():
                if not isinstance(info, dict):
                    continue
                if num not in self._drivers:
                    self._drivers[num] = {
                        "currentPosition": 0, "currentPoints": 0.0,
                        "predictedPosition": 0, "predictedPoints": 0.0,
                    }
                d = self._drivers[num]
                # Current* arrive only in the first full snapshot; later
                # messages are deltas that omit them, so retain the last
                # seen value (= the pre-race baseline). The difference
                # predicted − current = points expected to be won today.
                if "CurrentPosition" in info:
                    d["currentPosition"] = info["CurrentPosition"]
                    changed = True
                if "CurrentPoints" in info:
                    d["currentPoints"] = info["CurrentPoints"]
                    changed = True
                if "PredictedPosition" in info:
                    d["predictedPosition"] = info["PredictedPosition"]
                    changed = True
                if "PredictedPoints" in info:
                    d["predictedPoints"] = info["PredictedPoints"]
                    changed = True

        teams = data.get("Teams")
        if isinstance(teams, dict):
            for key, info in teams.items():
                if not isinstance(info, dict):
                    continue
                if key not in self._teams:
                    self._teams[key] = {
                        "teamName": "",
                        "currentPosition": 0, "currentPoints": 0.0,
                        "predictedPosition": 0, "predictedPoints": 0.0,
                    }
                t = self._teams[key]
                if "TeamName" in info:
                    t["teamName"] = info["TeamName"]
                if "CurrentPosition" in info:
                    t["currentPosition"] = info["CurrentPosition"]
                    changed = True
                if "CurrentPoints" in info:
                    t["currentPoints"] = info["CurrentPoints"]
                    changed = True
                if "PredictedPosition" in info:
                    t["predictedPosition"] = info["PredictedPosition"]
                    changed = True
                if "PredictedPoints" in info:
                    t["predictedPoints"] = info["PredictedPoints"]
                    changed = True

        if changed:
            self._emit(clock_time)

    @staticmethod
    def _row(key_field: str, key: str, d: dict) -> dict:
        cur_pts = d.get("currentPoints", 0.0)
        pred_pts = d.get("predictedPoints", 0.0)
        cur_pos = d.get("currentPosition", 0)
        pred_pos = d.get("predictedPosition", 0)
        return {
            key_field: key,
            "currentPoints": cur_pts,
            "predictedPoints": pred_pts,
            # points won today; >= 0 (championship points only accrue).
            "pointsToday": round(pred_pts - cur_pts, 1),
            "currentPosition": cur_pos,
            "predictedPosition": pred_pos,
            # +N = climbed N places (lower position number is better).
            "positionChange": (cur_pos - pred_pos) if (cur_pos and pred_pos) else 0,
        }

    def _emit(self, clock_time: datetime) -> None:
        drivers_sorted = sorted(
            self._drivers.items(),
            key=lambda x: x[1]["predictedPosition"],
        )
        drivers_out = [self._row("num", num, d) for num, d in drivers_sorted]

        teams_sorted = sorted(
            self._teams.values(),
            key=lambda x: x["predictedPosition"],
        )
        constructors_out = [self._row("team", t["teamName"], t) for t in teams_sorted]

        self._bus.emit("championshipPrediction", {
            "drivers": drivers_out,
            "constructors": constructors_out,
        }, clock_time)

    def snapshot(self) -> dict[str, Any]:
        import copy
        return {
            "drivers": copy.deepcopy(self._drivers),
            "teams": copy.deepcopy(self._teams),
        }

    def restore(self, state: dict[str, Any]) -> None:
        import copy
        self._drivers = copy.deepcopy(state.get("drivers", {}))
        self._teams = copy.deepcopy(state.get("teams", {}))

    def reset(self) -> None:
        self._drivers.clear()
        self._teams.clear()
