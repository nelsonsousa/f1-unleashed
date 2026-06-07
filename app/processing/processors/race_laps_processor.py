"""
Race Laps Processor — race progress with overlays.

Subscribes to: DriverList, LapCount, TimingData, TimingAppData, RaceControlMessages
Emits: display:race-laps

Ports the race laps logic from race_laps.js.
"""

import re
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

TEAM_COLORS = {
    "1": "#ff8000", "81": "#ff8000",
    "3": "#1e3d7b", "6": "#1e3d7b",
    "16": "#e8002d", "44": "#e8002d",
    "12": "#00d4be", "63": "#00d4be",
    "14": "#1a7a5a", "18": "#1a7a5a",
    "10": "#00a1e8", "43": "#00a1e8",
    "23": "#0f4c91", "55": "#0f4c91",
    "30": "#2d826d", "41": "#2d826d",
    "31": "#ffffff", "87": "#ffffff",
    "5": "#990000", "27": "#990000",
    "11": "#6e6e70", "77": "#6e6e70",
}
DEFAULT_CAR_COLOR = "#888888"

CAR_RE = re.compile(r"CAR (\d+)")


def _parse_lap_time_seconds(time_str: Optional[str]) -> float:
    if not time_str or not isinstance(time_str, str):
        return 0.0
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _create_driver(num: str) -> dict[str, Any]:
    return {
        "num": num,
        "tla": num,
        "color": TEAM_COLORS.get(num, DEFAULT_CAR_COLOR),
        "position": 99,
        "lapNumber": 0,
        "stints": [],
        "retired": False,
        "stopped": False,
        "penalties": [],
        "pendingPenalties": 0,
        "penaltyServedLaps": [],
        "fastestLaps": [],
    }


class RaceLapsProcessor(Processor):
    """Tracks race progress for race laps visualization."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict] = {}
        self._total_laps = 0
        self._current_lap = 0
        self._safety_car_periods: list[dict] = []
        self._active_sc: Optional[dict] = None
        self._red_flag_periods: list[dict] = []
        self._active_red_flag: Optional[dict] = None
        self._global_best_lap_time: Optional[float] = None

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("LapCount", self._handle_lap_count)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("TimingAppData", self._handle_timing_app_data)
        self._bus.on("RaceControlMessages", self._handle_rcm)

    def _get_driver(self, num: str) -> dict:
        if num not in self._drivers:
            self._drivers[num] = _create_driver(num)
        return self._drivers[num]

    def _is_lap_under_sc(self, lap: int) -> bool:
        for sc in self._safety_car_periods:
            if lap >= sc["startLap"] and lap <= sc["endLap"]:
                return True
        if self._active_sc and lap >= self._active_sc["startLap"]:
            return True
        return False

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, info in data.items():
            if not isinstance(info, dict):
                continue
            d = self._get_driver(num)
            if info.get("Tla"):
                d["tla"] = info["Tla"]
            if info.get("TeamColour"):
                d["color"] = f"#{info['TeamColour']}"
        self._emit(clock_time)

    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if data.get("TotalLaps") is not None:
            try:
                self._total_laps = int(data["TotalLaps"])
            except (ValueError, TypeError):
                pass
        if data.get("CurrentLap") is not None:
            try:
                self._current_lap = int(data["CurrentLap"])
            except (ValueError, TypeError):
                pass

        # Close active periods at race end
        if self._active_sc and self._current_lap >= self._total_laps:
            self._active_sc["endLap"] = self._total_laps
            self._safety_car_periods.append(self._active_sc)
            self._active_sc = None
        if self._active_red_flag and self._current_lap >= self._total_laps:
            self._active_red_flag["endLap"] = self._total_laps
            self._red_flag_periods.append(self._active_red_flag)
            self._active_red_flag = None

        self._emit(clock_time)

    def _handle_timing_data(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") or data if isinstance(data, dict) else None
        if not lines or not isinstance(lines, dict):
            return

        changed = False
        for num, timing in lines.items():
            if not isinstance(timing, dict):
                continue
            d = self._get_driver(num)

            if timing.get("NumberOfLaps") is not None:
                try:
                    new_lap = int(timing["NumberOfLaps"])
                except (ValueError, TypeError):
                    new_lap = 0
                if new_lap > d["lapNumber"]:
                    d["lapNumber"] = new_lap
                    changed = True

            if timing.get("Position") is not None:
                try:
                    d["position"] = int(timing["Position"])
                    changed = True
                except (ValueError, TypeError):
                    pass

            if timing.get("Retired") is not None:
                d["retired"] = bool(timing["Retired"])
                changed = True
            if timing.get("Stopped") is not None:
                d["stopped"] = bool(timing["Stopped"])
                changed = True

            # Penalty served on pit entry
            if timing.get("InPit") is True and d["pendingPenalties"] > 0:
                d["penaltyServedLaps"].append(d["lapNumber"])
                d["pendingPenalties"] = 0
                changed = True

            # Fastest lap tracking
            llt = timing.get("LastLapTime")
            if isinstance(llt, dict) and llt.get("Value"):
                lap_time = _parse_lap_time_seconds(llt["Value"])
                if lap_time > 0:
                    completed_lap = d["lapNumber"]
                    if (completed_lap > 1
                            and not self._is_lap_under_sc(completed_lap)
                            and (not self._global_best_lap_time
                                 or lap_time < self._global_best_lap_time)):
                        self._global_best_lap_time = lap_time
                        d["fastestLaps"].append(completed_lap)
                        changed = True

        if changed:
            self._emit(clock_time)

    def _handle_timing_app_data(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") or data if isinstance(data, dict) else None
        if not lines or not isinstance(lines, dict):
            return

        changed = False
        for num, app_data in lines.items():
            if not isinstance(app_data, dict):
                continue
            d = self._get_driver(num)
            stints = app_data.get("Stints")
            if not stints:
                continue

            entries = stints.items() if isinstance(stints, dict) else enumerate(stints)
            for idx, stint_info in entries:
                if not stint_info:
                    continue
                try:
                    i = int(idx)
                except (ValueError, TypeError):
                    continue

                while len(d["stints"]) <= i:
                    d["stints"].append(None)

                stint = d["stints"][i]
                if not stint:
                    stint = {"compound": None, "isNew": None, "startLap": None, "tyresNotChanged": False}
                    d["stints"][i] = stint

                if stint_info.get("Compound") is not None:
                    stint["compound"] = stint_info["Compound"]
                if stint_info.get("New") is not None and stint["isNew"] is None:
                    stint["isNew"] = stint_info["New"] in ("true", True)
                if stint_info.get("TyresNotChanged") is not None:
                    val = stint_info["TyresNotChanged"]
                    stint["tyresNotChanged"] = val in ("1", 1, True, "true")

                if stint["startLap"] is None:
                    if i == 0:
                        stint["startLap"] = 1
                    else:
                        stint["startLap"] = d["lapNumber"] + 1
                    changed = True

        if changed:
            self._emit(clock_time)

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        messages = data.get("Messages") or data
        if not isinstance(messages, dict):
            return

        changed = False
        for key, msg in messages.items():
            if not isinstance(msg, dict):
                continue

            category = msg.get("Category", "")
            text = (msg.get("Message") or "").upper()
            flag = (msg.get("Flag") or "").upper()
            try:
                lap = int(msg.get("Lap", 0))
            except (ValueError, TypeError):
                lap = self._current_lap

            # Safety car
            if category == "SafetyCar":
                status = (msg.get("Status") or "").upper()
                mode = (msg.get("Mode") or "").upper()

                if status == "DEPLOYED":
                    sc_type = "VSC" if "VIRTUAL" in mode else "SC"
                    if self._active_sc:
                        self._active_sc["endLap"] = lap or self._current_lap
                        self._safety_car_periods.append(self._active_sc)
                    self._active_sc = {
                        "type": sc_type,
                        "startLap": lap or self._current_lap,
                    }
                    changed = True
                elif status in ("IN THIS LAP", "ENDING"):
                    if self._active_sc:
                        self._active_sc["endLap"] = lap or self._current_lap
                        self._safety_car_periods.append(self._active_sc)
                        self._active_sc = None
                        changed = True

            # Red flag
            if category == "Flag" and (msg.get("Scope") or "").upper() == "TRACK":
                if flag == "RED":
                    if self._active_red_flag:
                        self._active_red_flag["endLap"] = lap or self._current_lap
                        self._red_flag_periods.append(self._active_red_flag)
                    self._active_red_flag = {"startLap": lap or self._current_lap}
                    changed = True
                elif flag in ("GREEN", "CLEAR"):
                    if self._active_red_flag:
                        self._active_red_flag["endLap"] = lap or self._current_lap
                        self._red_flag_periods.append(self._active_red_flag)
                        self._active_red_flag = None
                        changed = True

            # Penalties
            match = CAR_RE.search(text)
            if match:
                car_num = match.group(1)
                d = self._get_driver(car_num)

                if (("5 SECOND TIME PENALTY" in text
                     or "10 SECOND TIME PENALTY" in text
                     or "DRIVE THROUGH PENALTY" in text
                     or "STOP AND GO" in text
                     or "STOP/GO" in text)
                        and "PENALTY SERVED" not in text):
                    p_type = "5s"
                    if "10 SECOND" in text:
                        p_type = "10s"
                    elif "DRIVE THROUGH" in text:
                        p_type = "dt"
                    elif "STOP AND GO" in text or "STOP/GO" in text:
                        p_type = "sg"

                    d["penalties"].append({"lap": lap or self._current_lap, "type": p_type})
                    d["pendingPenalties"] += 1
                    changed = True

        if changed:
            self._emit(clock_time)

    def _emit(self, clock_time: datetime) -> None:
        drivers = []
        for d in sorted(self._drivers.values(), key=lambda x: x["position"]):
            if d["tla"] == d["num"]:
                continue
            stints = [s for s in d["stints"] if s]
            drivers.append({
                "num": d["num"],
                "tla": d["tla"],
                "color": d["color"],
                "lapNumber": d["lapNumber"],
                "retired": d["retired"],
                "stopped": d["stopped"],
                "stints": stints,
                "penalties": d["penalties"],
                "penaltyServedLaps": d["penaltyServedLaps"],
                "fastestLaps": d["fastestLaps"],
            })

        # Build all SC periods including active
        all_sc = list(self._safety_car_periods)
        if self._active_sc:
            all_sc.append({
                "type": self._active_sc["type"],
                "startLap": self._active_sc["startLap"],
                "endLap": self._current_lap,
            })

        all_rf = list(self._red_flag_periods)
        if self._active_red_flag:
            all_rf.append({
                "startLap": self._active_red_flag["startLap"],
                "endLap": self._current_lap,
            })

        self._bus.emit("display:race-laps", {
            "drivers": drivers,
            "totalLaps": self._total_laps,
            "currentLap": self._current_lap,
            "safetyCarPeriods": all_sc,
            "redFlagPeriods": all_rf,
            "globalBestLapTime": self._global_best_lap_time,
        }, clock_time)

    def snapshot(self) -> dict[str, Any]:
        import copy
        return {
            "drivers": copy.deepcopy(self._drivers),
            "total_laps": self._total_laps,
            "current_lap": self._current_lap,
            "safety_car_periods": copy.deepcopy(self._safety_car_periods),
            "active_sc": copy.deepcopy(self._active_sc),
            "red_flag_periods": copy.deepcopy(self._red_flag_periods),
            "active_red_flag": copy.deepcopy(self._active_red_flag),
            "global_best_lap_time": self._global_best_lap_time,
        }

    def restore(self, state: dict[str, Any]) -> None:
        import copy
        self._drivers = copy.deepcopy(state.get("drivers", {}))
        self._total_laps = state.get("total_laps", 0)
        self._current_lap = state.get("current_lap", 0)
        self._safety_car_periods = copy.deepcopy(state.get("safety_car_periods", []))
        self._active_sc = copy.deepcopy(state.get("active_sc"))
        self._red_flag_periods = copy.deepcopy(state.get("red_flag_periods", []))
        self._active_red_flag = copy.deepcopy(state.get("active_red_flag"))
        self._global_best_lap_time = state.get("global_best_lap_time")

    def reset(self) -> None:
        for d in self._drivers.values():
            d["position"] = 99
            d["lapNumber"] = 0
            d["stints"] = []
            d["retired"] = False
            d["stopped"] = False
            d["penalties"] = []
            d["pendingPenalties"] = 0
            d["penaltyServedLaps"] = []
            d["fastestLaps"] = []
        self._total_laps = 0
        self._current_lap = 0
        self._safety_car_periods = []
        self._active_sc = None
        self._red_flag_periods = []
        self._active_red_flag = None
        self._global_best_lap_time = None
