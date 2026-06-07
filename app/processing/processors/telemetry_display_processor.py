"""
Telemetry Display Processor — per-driver telemetry traces for display.

Subscribes to: CarData.z, Position.z, DriverList, TimingData, lap-status
Emits: display:telemetry

Wraps the existing TelemetryProcessor's internal state into display-ready
format with speed profiles indexed by track distance.
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

# Decimation: emit one sample per this many meters of track distance
SAMPLE_DIST_STEP = 10.0

# Maximum completed laps to keep in display payload per driver
MAX_COMPLETED_LAPS = 50


def _create_driver(num: str) -> dict:
    return {
        "num": num,
        "tla": num,
        "color": TEAM_COLORS.get(num, DEFAULT_CAR_COLOR),
        "lapNumber": 0,
        "lapStatus": "IN",
        "currentSamples": [],
        "completedLaps": [],
        "bestLapNumber": None,
        "bestLapTime": None,
    }


def _decimate_samples(samples: list[dict]) -> list[dict]:
    """Downsample to one point per SAMPLE_DIST_STEP meters."""
    if not samples:
        return []
    result = [samples[0]]
    last_dist = samples[0].get("dist", 0.0)
    for s in samples[1:]:
        d = s.get("dist", 0.0)
        if d - last_dist >= SAMPLE_DIST_STEP:
            result.append(s)
            last_dist = d
    # Always include last sample
    if len(samples) > 1 and result[-1] is not samples[-1]:
        result.append(samples[-1])
    return result


def _parse_lap_time(time_str: Optional[str]) -> Optional[float]:
    if not time_str or not isinstance(time_str, str):
        return None
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return None


class TelemetryDisplayProcessor(Processor):
    """Produces display-ready telemetry traces per driver."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict] = {}
        self._last_emit_lap: dict[str, int] = {}  # Track last emitted lap per driver
        self._sample_counter = 0

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("CarData.z", self._handle_car_data)
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
                    continue
                if new_lap > d["lapNumber"]:
                    # Finalize current lap
                    if d["currentSamples"]:
                        lap_time = None
                        llt = timing.get("LastLapTime")
                        if isinstance(llt, dict) and llt.get("Value"):
                            lap_time = _parse_lap_time(llt["Value"])

                        d["completedLaps"].append({
                            "lapNumber": d["lapNumber"],
                            "status": d["lapStatus"],
                            "lapTime": lap_time,
                            "samples": _decimate_samples(d["currentSamples"]),
                        })

                        # Track best
                        if lap_time and (not d["bestLapTime"] or lap_time < d["bestLapTime"]):
                            d["bestLapTime"] = lap_time
                            d["bestLapNumber"] = d["lapNumber"]

                        # Trim old laps
                        if len(d["completedLaps"]) > MAX_COMPLETED_LAPS:
                            d["completedLaps"] = d["completedLaps"][-MAX_COMPLETED_LAPS:]

                    d["currentSamples"] = []
                    d["lapNumber"] = new_lap
                    d["lapStatus"] = "UNK"
                    changed = True

            if timing.get("PitOut") is True:
                d["lapStatus"] = "OUT"
                changed = True
            if timing.get("InPit") is True:
                d["lapStatus"] = "IN"
                d["currentSamples"] = []
                changed = True

            # Update last lap time if it arrives separately
            if isinstance(timing.get("LastLapTime"), dict):
                lt = _parse_lap_time(timing["LastLapTime"].get("Value"))
                if lt and d["completedLaps"]:
                    last = d["completedLaps"][-1]
                    if not last["lapTime"]:
                        last["lapTime"] = lt
                        if not d["bestLapTime"] or lt < d["bestLapTime"]:
                            d["bestLapTime"] = lt
                            d["bestLapNumber"] = last["lapNumber"]
                        changed = True

        if changed:
            self._emit(clock_time)

    def _handle_car_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        entries_list = data.get("Entries")
        if not isinstance(entries_list, list):
            return

        for entry in entries_list:
            if not isinstance(entry, dict):
                continue
            cars = entry.get("Cars")
            if not isinstance(cars, dict):
                continue

            for num, car in cars.items():
                if not isinstance(car, dict):
                    continue
                d = self._get_driver(num)
                if d["lapStatus"] == "IN":
                    continue

                channels = car.get("Channels")
                if not isinstance(channels, dict):
                    continue

                speed = channels.get("0", 0)
                throttle = channels.get("4", 0)
                brake = channels.get("5", 0)
                gear = channels.get("3", 0)

                self._sample_counter += 1
                d["currentSamples"].append({
                    "speed": speed,
                    "throttle": throttle / 100.0 if throttle else 0.0,
                    "brake": brake / 100.0 if brake else 0.0,
                    "gear": gear,
                })

        # Emit periodically (~1Hz) rather than at every 4Hz sample
        if self._sample_counter % 16 == 0:
            self._emit(clock_time)

    def _handle_lap_status(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        num = data.get("driverNum")
        status = data.get("status")
        if not num or not status:
            return
        d = self._get_driver(num)
        d["lapStatus"] = status
        self._emit(clock_time)

    def _emit(self, clock_time: datetime) -> None:
        drivers = {}
        for num, d in self._drivers.items():
            if d["tla"] == d["num"]:
                continue
            drivers[num] = {
                "tla": d["tla"],
                "color": d["color"],
                "currentLap": {
                    "lapNumber": d["lapNumber"],
                    "status": d["lapStatus"],
                    "sampleCount": len(d["currentSamples"]),
                },
                "completedLapCount": len(d["completedLaps"]),
                "bestLap": {
                    "lapNumber": d["bestLapNumber"],
                    "lapTime": d["bestLapTime"],
                } if d["bestLapNumber"] else None,
            }

        self._bus.emit("display:telemetry", {
            "drivers": drivers,
        }, clock_time)

    def snapshot(self) -> dict[str, Any]:
        import copy
        return {
            "drivers": copy.deepcopy(self._drivers),
            "sample_counter": self._sample_counter,
        }

    def restore(self, state: dict[str, Any]) -> None:
        import copy
        self._drivers = copy.deepcopy(state.get("drivers", {}))
        self._sample_counter = state.get("sample_counter", 0)

    def reset(self) -> None:
        for d in self._drivers.values():
            d["lapNumber"] = 0
            d["lapStatus"] = "IN"
            d["currentSamples"] = []
            d["completedLaps"] = []
            d["bestLapNumber"] = None
            d["bestLapTime"] = None
        self._sample_counter = 0
