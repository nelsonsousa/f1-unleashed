"""
Driver Status Processor — single per-driver status from TimingData flags.

Subscribes to: TimingData
Emits: driverStatus:{num}  — "RET" | "STOP" | "OUT" | "PIT" | "TRACK"

Tracks four booleans per driver from TimingData (Retired, Stopped, PitOut,
InPit) — each set when its field is True, cleared when False — and emits the
highest-priority active one:

    RET > STOP > OUT > PIT > TRACK

TRACK = no flag active (the client renders it as an empty badge). PIT and OUT
are mutually exclusive in the source. This is the sole emitter of
driverStatus:{num} (the RET/STOP emission formerly in timing_processor is
removed to avoid two processors writing the same topic).
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# (F1 field, internal flag) in priority order — first active wins.
_FIELDS = (
    ("Retired", "retired"),
    ("Stopped", "stopped"),
    ("PitOut", "pitOut"),
    ("InPit", "inPit"),
)


class DriverStatusProcessor(Processor):
    """Per-driver status as the priority-max of the four TimingData flags."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._flags: dict[str, dict] = {}   # num -> {retired,stopped,pitOut,inPit}
        self._last: dict[str, str] = {}      # num -> last emitted status

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            f = self._flags.setdefault(
                num, {"retired": False, "stopped": False, "pitOut": False, "inPit": False})
            touched = False
            for src, key in _FIELDS:
                if src in d:
                    v = bool(d[src])
                    if f[key] != v:
                        f[key] = v
                        touched = True
            if not touched:
                continue
            status = self._status(f)
            if self._last.get(num) != status:
                self._last[num] = status
                self._bus.emit(f"driverStatus:{num}", status, clock_time)

    @staticmethod
    def _status(f: dict) -> str:
        if f["retired"]:
            return "RET"
        if f["stopped"]:
            return "STOP"
        if f["pitOut"]:
            return "OUT"
        if f["inPit"]:
            return "PIT"
        return "TRACK"
