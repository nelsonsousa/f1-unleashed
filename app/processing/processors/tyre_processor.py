"""
Tyre Processor — current tyre + past-stint history per driver.

Subscribes to: TimingAppData (Stints)
Emits:
  currentTyre:{num}   {compound, isNew, age}             — the running stint
                      (age = current total laps on the tyre, +1 each lap)
  tyreHistory:{num}   [ {compound, totalLaps, isNew} ]   — past stints (excl current)

F1 TimingAppData.Stints[i] fields:
  Compound, New ("true"/"false"),
  TotalLaps (running total tyre age = laps already on it + laps driven).
Highest stint index = current tyre; lower indices are past tyres, finalised
with their last-known TotalLaps. Replaces the dead tyre_history_processor.
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _as_bool(v: Any) -> bool:
    return v in (True, "true", "True", 1, "1")


class TyreProcessor(Processor):
    """Current tyre + past-stint history per driver, from TimingAppData stints."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._stints: dict[str, dict[int, dict]] = {}

    def subscribe(self) -> None:
        self._bus.on("TimingAppData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            sp = d.get("Stints")
            if not sp:
                continue
            stints = self._stints.setdefault(num, {})
            items = sp.items() if isinstance(sp, dict) else enumerate(sp)
            changed = False
            for idx, st in items:
                if not isinstance(st, dict):
                    continue
                i = int(idx)
                cur = stints.setdefault(
                    i, {"compound": None, "isNew": False, "totalLaps": 0})
                if st.get("Compound"):
                    cur["compound"] = st["Compound"]; changed = True
                if "New" in st:
                    cur["isNew"] = _as_bool(st["New"]); changed = True
                if st.get("TotalLaps") is not None:
                    try:
                        cur["totalLaps"] = int(st["TotalLaps"]); changed = True
                    except (TypeError, ValueError):
                        pass
            if changed:
                self._emit(num, clock_time)

    def _emit(self, num: str, clock_time: datetime) -> None:
        stints = self._stints[num]
        idxs = [i for i in sorted(stints) if stints[i]["compound"]]   # real stints only
        if not idxs:
            return
        cur = stints[idxs[-1]]
        self._bus.emit(f"currentTyre:{num}", {
            "compound": cur["compound"],
            "isNew": cur["isNew"],
            "age": cur["totalLaps"],   # current tyre age — F1 TotalLaps, +1 each lap
        }, clock_time)
        history = [{
            "compound": stints[i]["compound"],
            "totalLaps": stints[i]["totalLaps"],
            "isNew": stints[i]["isNew"],
        } for i in idxs[:-1]]
        self._bus.emit(f"tyreHistory:{num}", history, clock_time)
