"""
P/Q Last-Lap Colour Processor — colours each driver's LAST lap against the fastest
overall lap of the current part (practice + qualifying only).

Subscribes (wildcard) to: driverLaps:{num}, driverLapClassification:{num},
                          qualifyingPart
Emits: driverPaceColour:{num}  { lap, colour }   (persisted; same topic race_pace
       uses for the race last-lap — the two never run in the same session)

Reference = the fastest overall lap of the CURRENT part = min bestLap across
drivers whose bestLap belongs to the current part (bestLap.part; None outside
quali). Bands (Δ = last − reference):
    == reference     purple
    < 0.2            blue
    < 0.5            green
    < 1.0            yellow
    practice: < 2.0  orange, else red (full time-based scale)
    qualifying:      red when the last lap is SLOWER than the BUBBLE (the cutoff
                     car's best — 16th fastest in Q1, 10th in Q2); else orange
                     (capped — red is bubble-based, not >2s).
In/out/stop laps are WHITE. The whole active field is recomputed whenever the
reference or bubble changes; a single driver on a new last lap / classification.
Eliminated drivers show no colour — lap_timing scopes their last-lap to the part,
so their lastLap is null in the next part (the cell is blank).
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# Lap classifications that are NOT representative timed laps → white.
NON_REPRESENTATIVE = {"PIT", "OUT", "STOP"}


def _parse_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


class PQPaceProcessor(Processor):
    """Per-driver last-lap colour vs the fastest overall (practice + qualifying)."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type in ("practice", "qualifying")
        self._is_quali = session_type == "qualifying"
        self._best: dict[str, dict] = {}    # num -> {"ms", "part"}
        self._last: dict[str, dict] = {}    # num -> {"lap", "ms"}
        self._cls: dict[str, dict[int, str]] = {}
        self._part: Optional[int] = None
        self._ref_ms: Optional[int] = None
        self._bubble_ms: Optional[int] = None
        self._status: dict[str, Optional[str]] = {}
        self._colour: dict[str, Optional[str]] = {}   # dedup

    def subscribe(self) -> None:
        if self._active:
            self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLapClassification:"):
            self._on_cls(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:"):
            num = topic.split(":", 1)[1]
            st = data if isinstance(data, str) else None
            if st != self._status.get(num):
                self._status[num] = st
                self._emit(num, clock_time)
        elif topic == "qualifyingPart":
            self._on_part(data, clock_time)

    def _on_part(self, data: Any, clock_time: datetime) -> None:
        part = data if isinstance(data, int) else None
        if part == self._part:
            return
        self._part = part
        self._reprice(clock_time)

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        ref_changed = False
        if "bestLap" in data:
            bl = data["bestLap"]
            if bl and bl.get("time"):
                ms = _parse_ms(bl["time"])
                if ms is not None and self._best.get(num, {}).get("ms") != ms:
                    self._best[num] = {"ms": ms, "part": bl.get("part")}
                    ref_changed = True
            elif num in self._best:                 # bestLap null → cleared
                del self._best[num]
                ref_changed = True
        last_changed = False
        if "lastLap" in data:
            ll = data["lastLap"]
            if ll and ll.get("time"):
                ms = _parse_ms(ll["time"])
                if ms is not None:
                    self._last[num] = {"lap": ll.get("lap"), "ms": ms}
                    last_changed = True
            elif num in self._last:                 # lastLap null → cleared
                del self._last[num]
                last_changed = True
        if ref_changed:
            self._reprice(clock_time)               # recolours the field if it moved
        # Always (re)emit THIS driver — a new best/last that didn't move the
        # reference/bubble still needs its own colour, and _reprice dedups.
        if ref_changed or last_changed:
            self._emit(num, clock_time)

    def _on_cls(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lap = data.get("lap")
        if isinstance(lap, int):
            self._cls.setdefault(num, {})[lap] = data.get("type", "")
            self._emit(num, clock_time)

    def _current_part_bests(self) -> list:
        return [b["ms"] for b in self._best.values()
                if self._part is None or b.get("part") == self._part]

    def _cutoff_pos(self) -> Optional[int]:
        return 16 if self._part == 1 else 10 if self._part == 2 else None

    def _reprice(self, clock_time: datetime) -> None:
        bests = self._current_part_bests()
        new_ref = min(bests) if bests else None
        cut = self._cutoff_pos()
        new_bubble = sorted(bests)[cut - 1] if cut is not None and len(bests) >= cut else None
        if new_ref == self._ref_ms and new_bubble == self._bubble_ms:
            return
        self._ref_ms = new_ref
        self._bubble_ms = new_bubble
        for num in list(self._last):
            self._emit(num, clock_time)

    def _colour_for(self, num: str) -> Optional[str]:
        last = self._last.get(num)
        if not last:
            return None
        # Suppression → "blank" (client blanks the value). Retired/finished/
        # eliminated (all), and P/Q out/in/slow laps. (ybTVoVep)
        if self._status.get(num) in ("RET", "STOP", "DSQ", "FINISHED", "ELIMINATED"):
            return "blank"
        if self._cls.get(num, {}).get(last["lap"]) in ("OUT", "PIT", "SLOW"):
            return "blank"
        if self._ref_ms is None:
            return "white"
        ms = last["ms"]
        if ms <= self._ref_ms:
            return "purple"                         # last lap is the fastest overall
        if self._is_quali and self._bubble_ms is not None and ms > self._bubble_ms:
            return "red"                            # slower than the bubble → wouldn't advance
        delta = (ms - self._ref_ms) / 1000.0
        if delta < 0.2:
            return "blue"
        if delta < 0.5:
            return "green"
        if delta < 1.0:
            return "yellow"
        if self._is_quali:
            return "orange"                         # capped (red is bubble-based)
        return "orange" if delta < 2.0 else "red"

    def _emit(self, num: str, clock_time: datetime) -> None:
        colour = self._colour_for(num)
        if self._colour.get(num) == colour:
            return
        self._colour[num] = colour
        last = self._last.get(num)
        self._bus.emit(f"driverPaceColour:{num}",
                       {"lap": last["lap"] if last else None, "colour": colour}, clock_time)
