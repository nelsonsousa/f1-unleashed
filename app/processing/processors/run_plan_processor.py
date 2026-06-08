"""
Run Plan Processor — lap grid per driver with classification.

Subscribes to: DriverList, TimingData, TimingAppData, SessionData, lap-status
Emits: display:run-plan

Ports the run plan logic from run_plan.js.
"""

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


def _parse_lap_time(time_str: Optional[str]) -> Optional[float]:
    """Parse lap time to seconds. Returns None on failure."""
    if not time_str or not isinstance(time_str, str):
        return None
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def _create_driver(num: str) -> dict[str, Any]:
    return {
        "num": num,
        "tla": num,
        "color": TEAM_COLORS.get(num, DEFAULT_CAR_COLOR),
        "position": 99,
        "completedLaps": [],
        "bestLapTime": None,
        "lapStatus": "IN",
        "pendingLapStatus": None,
        "currentCompound": None,
        "currentTyreLaps": 0,
        "lapNumber": 0,
        "onTrackSeen": False,
    }


class RunPlanProcessor(Processor):
    """Computes the run plan (lap grid) for all drivers."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict] = {}
        self._global_best_lap_time: Optional[float] = None
        self._qualifying_segment: Optional[str] = None
        self._segment_boundaries: list[dict[str, int]] = []

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("TimingAppData", self._handle_timing_app_data)
        self._bus.on("SessionData", self._handle_session_data)
        self._bus.on("~lap-status", self._handle_lap_status)

    def _get_driver(self, num: str) -> dict:
        if num not in self._drivers:
            self._drivers[num] = _create_driver(num)
        return self._drivers[num]

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
                    if d["lapNumber"] > 0 or d["onTrackSeen"]:
                        lap_time = None
                        llt = timing.get("LastLapTime")
                        if isinstance(llt, dict) and llt.get("Value"):
                            lap_time = _parse_lap_time(llt["Value"])
                        d["completedLaps"].append({
                            "status": d["lapStatus"],
                            "lapTime": lap_time,
                            "compound": d["currentCompound"],
                            "tyreLaps": d["currentTyreLaps"],
                        })
                    d["lapNumber"] = new_lap

                    pls = d["pendingLapStatus"]
                    if pls and pls["lapNumber"] == new_lap:
                        d["lapStatus"] = pls["status"]
                        d["pendingLapStatus"] = None
                    else:
                        d["lapStatus"] = "UNK"
                        d["pendingLapStatus"] = None
                    changed = True

            if timing.get("PitOut") is True:
                d["lapStatus"] = "OUT"
                d["onTrackSeen"] = True
                changed = True

            if timing.get("InPit") is True:
                d["lapStatus"] = "IN"
                changed = True

            if isinstance(timing.get("LastLapTime"), dict):
                llt_val = timing["LastLapTime"].get("Value")
                lt = _parse_lap_time(llt_val)
                if lt and lt > 0:
                    last = d["completedLaps"][-1] if d["completedLaps"] else None
                    if last and not last["lapTime"]:
                        last["lapTime"] = lt
                        changed = True
                    if not d["bestLapTime"] or lt < d["bestLapTime"]:
                        d["bestLapTime"] = lt
                    if not self._global_best_lap_time or lt < self._global_best_lap_time:
                        self._global_best_lap_time = lt

            if timing.get("Position") is not None:
                try:
                    d["position"] = int(timing["Position"])
                    changed = True
                except (ValueError, TypeError):
                    pass

            if isinstance(timing.get("BestLapTime"), dict):
                blt_val = timing["BestLapTime"].get("Value")
                bt = _parse_lap_time(blt_val)
                if bt and bt > 0:
                    if not d["bestLapTime"] or bt < d["bestLapTime"]:
                        d["bestLapTime"] = bt
                    if not self._global_best_lap_time or bt < self._global_best_lap_time:
                        self._global_best_lap_time = bt
                    changed = True

        if changed:
            self._emit(clock_time)

    def _handle_lap_status(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        num = data.get("driverNum")
        lap_number = data.get("lapNumber")
        status = data.get("status")
        if not num or not lap_number or not status:
            return

        d = self._get_driver(num)
        if d["lapNumber"] == lap_number:
            d["lapStatus"] = status
            self._emit(clock_time)
        else:
            d["pendingLapStatus"] = {"lapNumber": lap_number, "status": status}

    def _handle_timing_app_data(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") or data if isinstance(data, dict) else None
        if not lines or not isinstance(lines, dict):
            return

        for num, app_data in lines.items():
            if not isinstance(app_data, dict):
                continue
            d = self._get_driver(num)
            stints = app_data.get("Stints")
            if not stints:
                continue

            entries = stints.items() if isinstance(stints, dict) else enumerate(stints)
            for idx, stint in entries:
                if not stint:
                    continue
                if stint.get("Compound"):
                    d["currentCompound"] = stint["Compound"]
                if stint.get("TotalLaps") is not None:
                    try:
                        d["currentTyreLaps"] = int(stint["TotalLaps"])
                    except (ValueError, TypeError):
                        pass

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        series = data.get("Series")
        if not series or not isinstance(series, dict):
            return

        entries = list(series.items())
        if not entries:
            return

        latest = entries[-1][1]
        qp = latest.get("QualifyingPart")
        if not qp or qp < 1 or qp > 3:
            return

        segment = f"Q{qp}"
        if segment == self._qualifying_segment:
            return

        # Segment changed — record boundary
        if self._qualifying_segment:
            for d in self._drivers.values():
                if d["lapNumber"] > 0 or d["onTrackSeen"]:
                    d["completedLaps"].append({
                        "status": d["lapStatus"],
                        "lapTime": None,
                        "compound": d["currentCompound"],
                        "tyreLaps": d["currentTyreLaps"],
                    })
                    d["onTrackSeen"] = False
                    d["lapStatus"] = "IN"
            boundary = {num: len(d["completedLaps"]) for num, d in self._drivers.items()}
            self._segment_boundaries.append(boundary)

        self._qualifying_segment = segment
        self._emit(clock_time)

    def _emit(self, clock_time: datetime) -> None:
        drivers = []
        for d in sorted(self._drivers.values(), key=lambda x: x["position"]):
            if d["tla"] == d["num"]:
                continue
            show_current = d["lapNumber"] > 0 or d["onTrackSeen"]
            laps = [{"status": l["status"], "lapTime": l["lapTime"]}
                    for l in d["completedLaps"]]
            if show_current:
                laps.append({"status": d["lapStatus"], "isCurrent": True, "lapTime": None})

            drivers.append({
                "num": d["num"],
                "tla": d["tla"],
                "color": d["color"],
                "position": d["position"],
                "laps": laps,
                "currentLapStatus": d["lapStatus"],
                "bestLapTime": d["bestLapTime"],
            })

        self._bus.emit("display:run-plan", {
            "drivers": drivers,
            "globalBestLapTime": self._global_best_lap_time,
            "qualifyingSegment": self._qualifying_segment,
            "segmentBoundaries": self._segment_boundaries,
        }, clock_time)
