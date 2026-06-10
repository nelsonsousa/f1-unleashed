"""
Championship Prediction Processor — races only (Race + Sprint).

Subscribes to: ChampionshipPrediction, driverList
Emits (persisted), sorted by predicted position:
  championshipDrivers      [ {driverNumber, teamColour, driverName,
                              predictedPosition, predictedPoints,
                              pointsGained, positionsGained} ]
  championshipConstructors [ {teamName, teamColour, predictedPosition,
                              predictedPoints, pointsGained, positionsGained} ]

  pointsGained    = predictedPoints - currentPoints
  positionsGained = currentPosition - predictedPosition  (+N = climbed N places)

driverName is the driver's full LastName (from driverList), not the TLA. All
ChampionshipPrediction fields are sticky F1 deltas — merged, never reset.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

_FIELDS = ("CurrentPosition", "PredictedPosition", "CurrentPoints", "PredictedPoints")


class ChampionshipProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type in ("race", "sprint")
        self._drivers: dict[str, dict] = {}   # num -> sticky prediction fields
        self._teams: dict[str, dict] = {}      # teamName -> sticky prediction fields
        self._dl: dict[str, dict] = {}         # num -> {name, colour, team}

    def subscribe(self) -> None:
        if not self._active:
            return
        self._bus.on("ChampionshipPrediction", self._handle)
        self._bus.on("driverList", self._handle_driver_list)

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, info in data.items():
            if isinstance(info, dict):
                self._dl[num] = {
                    "name": info.get("lastName") or info.get("tla") or num,
                    "colour": info.get("color"),
                    "team": info.get("teamName"),
                }
        # Names/colours may arrive after the first prediction — re-emit.
        if self._drivers or self._teams:
            self._emit(clock_time)

    @staticmethod
    def _merge(store: dict, key: str, entry: dict) -> bool:
        cur = store.setdefault(key, {})
        changed = False
        for f in _FIELDS:
            if f in entry and cur.get(f) != entry[f]:
                cur[f] = entry[f]
                changed = True
        return changed

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        changed = False
        drivers = data.get("Drivers")
        if isinstance(drivers, dict):
            for num, entry in drivers.items():
                if isinstance(entry, dict):
                    changed |= self._merge(self._drivers, num, entry)
        teams = data.get("Teams")
        if isinstance(teams, dict):
            for tk, entry in teams.items():
                if isinstance(entry, dict):
                    name = entry.get("TeamName") or tk
                    changed |= self._merge(self._teams, name, entry)
        if changed:
            self._emit(clock_time)

    @staticmethod
    def _gains(p: dict) -> tuple[Optional[float], Optional[int]]:
        pts = pos = None
        if p.get("PredictedPoints") is not None and p.get("CurrentPoints") is not None:
            pts = p["PredictedPoints"] - p["CurrentPoints"]
        if p.get("PredictedPosition") is not None and p.get("CurrentPosition") is not None:
            pos = p["CurrentPosition"] - p["PredictedPosition"]
        return pts, pos

    def _team_colour(self, team: Optional[str]) -> Optional[str]:
        for d in self._dl.values():
            if d.get("team") == team:
                return d.get("colour")
        return None

    def _emit(self, clock_time: datetime) -> None:
        drivers = []
        for num, p in self._drivers.items():
            pts, pos = self._gains(p)
            dl = self._dl.get(num, {})
            drivers.append({
                "driverNumber": num,
                "teamColour": dl.get("colour"),
                "driverName": dl.get("name", num),
                "predictedPosition": p.get("PredictedPosition"),
                "predictedPoints": p.get("PredictedPoints"),
                "pointsGained": pts,
                "positionsGained": pos,
            })
        drivers.sort(key=lambda d: (d["predictedPosition"] is None, d["predictedPosition"]))

        constructors = []
        for team, p in self._teams.items():
            pts, pos = self._gains(p)
            constructors.append({
                "teamName": team,
                "teamColour": self._team_colour(team),
                "predictedPosition": p.get("PredictedPosition"),
                "predictedPoints": p.get("PredictedPoints"),
                "pointsGained": pts,
                "positionsGained": pos,
            })
        constructors.sort(key=lambda c: (c["predictedPosition"] is None, c["predictedPosition"]))

        self._bus.emit("championshipDrivers", drivers, clock_time)
        self._bus.emit("championshipConstructors", constructors, clock_time)
