"""
Driver Status Processor — pit/track/out status per driver.

Subscribes to: TimingData, DriverList
Emits: driverStatus:{driverNum}  (value: "PIT", "OUT", or "TRACK")

All drivers start as PIT. Transitions:
  PitOut=true   -> OUT
  PitOut=false  -> TRACK
  InPit=true    -> PIT
  InPit=false   -> TRACK   (race grid release etc. — F1 doesn't always
                            send a paired PitOut for race start)
  NumberOfLaps≥1 + status==PIT
                -> TRACK   (driver has completed a lap → must be on
                            track; covers race starts where neither
                            InPit:false nor PitOut:false ever arrive)
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class DriverStatusProcessor(Processor):
    """Tracks pit/track status per driver."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._status: dict[str, str] = {}  # num -> "PIT"|"OUT"|"TRACK"
        # True once the driver has left the initial PIT state at least
        # once. After that, the "PIT + NumberOfLaps>=1 → TRACK"
        # fallback must NOT fire — mid-session pit stops have proper
        # InPit:false / PitOut:true transitions that we mustn't shadow.
        self._left_initial_pit: dict[str, bool] = {}

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("TimingData", self._handle_timing_data)

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, info in data.items():
            if not isinstance(info, dict):
                continue
            if num not in self._status:
                self._status[num] = "PIT"
                self._bus.emit(f"driverStatus:{num}", "PIT", clock_time)

    def _handle_timing_data(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") or data if isinstance(data, dict) else None
        if not lines or not isinstance(lines, dict):
            return

        for num, timing in lines.items():
            if not isinstance(timing, dict):
                continue

            new_status = None
            if timing.get("InPit") is True:
                new_status = "PIT"
            elif timing.get("PitOut") is True:
                new_status = "OUT"
            elif timing.get("PitOut") is False:
                new_status = "TRACK"
            elif timing.get("InPit") is False:
                new_status = "TRACK"
            else:
                # Fallback: a driver still flagged PIT who has completed
                # a lap must actually be on track — handles race starts
                # where F1 doesn't send a paired InPit:false / PitOut:false.
                # Only fires for drivers still in their initial PIT state
                # (= never transitioned to TRACK/OUT). After that,
                # mid-session pit stops use the explicit InPit/PitOut
                # signals.
                laps_val = timing.get("NumberOfLaps")
                if (laps_val is not None
                        and self._status.get(num) == "PIT"
                        and not self._left_initial_pit.get(num, False)):
                    try:
                        if int(laps_val) >= 1:
                            new_status = "TRACK"
                    except (TypeError, ValueError):
                        pass

            if new_status and new_status != self._status.get(num):
                if new_status in ("TRACK", "OUT"):
                    self._left_initial_pit[num] = True
                self._status[num] = new_status
                self._bus.emit(f"driverStatus:{num}", new_status, clock_time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": dict(self._status),
            "left_initial_pit": dict(self._left_initial_pit),
        }

    def restore(self, state: dict[str, Any]) -> None:
        self._status = state.get("status", {})
        self._left_initial_pit = state.get("left_initial_pit", {})

    def reset(self) -> None:
        self._status.clear()
        self._left_initial_pit.clear()
