"""
Tyre History Processor — tyre sets per driver with deduplication.

Subscribes to: DriverList, TimingData, TimingAppData
Emits: display:tyre-history

Ports the tyre history logic from tyre_history.js.
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


def _create_driver(num: str) -> dict[str, Any]:
    return {
        "num": num,
        "tla": num,
        "color": TEAM_COLORS.get(num, DEFAULT_CAR_COLOR),
        "position": 99,
        "rawStints": [],
        "tyreSets": [],
    }


def _rebuild_tyre_sets(driver: dict) -> None:
    """Rebuild deduplicated tyre sets from raw stints."""
    sets = []
    for raw in driver["rawStints"]:
        if not raw["compound"] or raw["compound"] == "UNKNOWN":
            continue

        if raw["isNew"]:
            sets.append({
                "compound": raw["compound"],
                "wasNew": True,
                "laps": raw["laps"],
            })
        else:
            matched = False
            # Exact match: accumulated laps equal this stint's starting laps
            for s in sets:
                if s["compound"] == raw["compound"] and s["laps"] == raw["initialLaps"]:
                    s["laps"] = raw["laps"]
                    matched = True
                    break
            # Match a 0-lap set of same compound
            if not matched:
                for s in sets:
                    if s["compound"] == raw["compound"] and s["laps"] == 0:
                        s["laps"] = raw["laps"]
                        matched = True
                        break
            # Match same compound and same laps (pit transition)
            if not matched:
                for s in sets:
                    if s["compound"] == raw["compound"] and s["laps"] == raw["laps"]:
                        matched = True
                        break
            if not matched:
                sets.append({
                    "compound": raw["compound"],
                    "wasNew": False,
                    "laps": raw["laps"],
                })

    driver["tyreSets"] = [s for s in sets if s["laps"] > 0]


class TyreHistoryProcessor(Processor):
    """Tracks tyre usage history per driver."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict] = {}

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("TimingAppData", self._handle_timing_app_data)

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
            if timing.get("Position") is not None:
                try:
                    d["position"] = int(timing["Position"])
                    changed = True
                except (ValueError, TypeError):
                    pass
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
            for idx, stint in entries:
                if not stint:
                    continue
                try:
                    stint_num = int(idx)
                except (ValueError, TypeError):
                    continue

                while len(d["rawStints"]) <= stint_num:
                    d["rawStints"].append({
                        "compound": None, "isNew": False,
                        "laps": 0, "initialLaps": None,
                    })

                raw = d["rawStints"][stint_num]
                if stint.get("Compound"):
                    raw["compound"] = stint["Compound"]
                    changed = True
                if stint.get("New") is not None:
                    raw["isNew"] = stint["New"] in ("true", True)
                    changed = True
                if stint.get("StartLaps") is not None:
                    try:
                        raw["initialLaps"] = int(stint["StartLaps"])
                    except (ValueError, TypeError):
                        pass
                if stint.get("TotalLaps") is not None:
                    try:
                        laps = int(stint["TotalLaps"])
                    except (ValueError, TypeError):
                        laps = 0
                    if raw["initialLaps"] is None and laps > 0:
                        raw["initialLaps"] = laps
                    raw["laps"] = laps
                    changed = True

            if changed:
                _rebuild_tyre_sets(d)

        if changed:
            self._emit(clock_time)

    def _emit(self, clock_time: datetime) -> None:
        drivers = []
        for d in sorted(self._drivers.values(), key=lambda x: x["position"]):
            if d["tla"] == d["num"]:
                continue
            drivers.append({
                "num": d["num"],
                "tla": d["tla"],
                "color": d["color"],
                "position": d["position"],
                "tyreSets": d["tyreSets"],
            })

        self._bus.emit("display:tyre-history", {
            "drivers": drivers,
        }, clock_time)

    def snapshot(self) -> dict[str, Any]:
        import copy
        return {"drivers": copy.deepcopy(self._drivers)}

    def restore(self, state: dict[str, Any]) -> None:
        import copy
        self._drivers = copy.deepcopy(state.get("drivers", {}))

    def reset(self) -> None:
        for d in self._drivers.values():
            d["position"] = 99
            d["rawStints"] = []
            d["tyreSets"] = []
