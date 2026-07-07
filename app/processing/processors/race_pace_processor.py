"""
Race Pace Processor — colour-codes each driver's last lap time against the FASTEST
lap on the SAME lap number (RACE ONLY).  Trello: "Current race pace".

Subscribes (wildcard) to: driverLaps:{num}, driverLapClassification:{num}
Emits: driverPaceColour:{num}  { lap, colour }   (persisted)

Reference = the fastest REPRESENTATIVE lap time on that lap number — min across the
field of each driver's lap-N time, excluding in-laps (PIT), out-laps (OUT) and STOP.
Stored per lap, so it's stable (monotonic — only improves) and apples-to-apples
(same fuel/tyre phase). It is NOT the leader's time: the leader can be pace-managing
while a car behind sets the fastest lap. Each driver's last representative lap is
compared to its lap's fastest and binned (delta = driver − fastest-on-that-lap):

    delta <= 0           purple   (the fastest on that lap)
    +0.001 .. +0.250     blue
    +0.251 .. +0.500     green
    +0.501 .. +1.000     yellow
    +1.001 .. +2.000     orange
    > +2.000             red

A driver whose last lap is an in/out/stop lap is WHITE (2026-07 SME rule; supersedes
qKVcxF9n); retired/stopped white, DSQ blank. "white" is also used before any
same-lap reference exists. The field is recomputed whenever a driver improves the
fastest time on its lap.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# Lap classifications that are NOT representative racing laps.
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


class RacePaceProcessor(Processor):
    """Per-driver last-lap pace colour vs the race leader (race only)."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._cls: dict[str, dict[int, str]] = {}   # num -> {lap -> classification type}
        self._last: dict[str, dict] = {}            # num -> {"lap", "ms"} last completed lap
        self._fastest_lap_ms: dict[int, int] = {}   # lap -> fastest representative lap time on it
        self._colour: dict[str, str] = {}           # num -> last emitted colour (dedup)
        self._status: dict[str, Optional[str]] = {}

    def subscribe(self) -> None:
        if self._is_race:
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
                self._emit_colour(num, clock_time)

    def _representative(self, num: str, lap: Optional[int]) -> bool:
        if lap is None:
            return False
        return self._cls.get(num, {}).get(lap, "") not in NON_REPRESENTATIVE

    def _band(self, delta_ms: int) -> str:
        if delta_ms <= 0:
            return "purple"
        s = delta_ms / 1000.0
        if s <= 0.250:
            return "blue"
        if s <= 0.500:
            return "green"
        if s <= 1.000:
            return "yellow"
        if s <= 2.000:
            return "orange"
        return "red"

    def _on_cls(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lap = data.get("lap")
        if not isinstance(lap, int):
            return
        self._cls.setdefault(num, {})[lap] = data.get("type", "")
        # Classification just resolved for the driver's last completed lap — it may
        # now be representative and improve that lap's fastest time.
        last = self._last.get(num)
        if last and last["lap"] == lap:
            if self._representative(num, lap) and self._update_fastest(lap, last["ms"]):
                self._recompute_all(clock_time)
            else:
                self._emit_colour(num, clock_time)

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        ll = data.get("lastLap")
        if not (isinstance(ll, dict) and ll.get("lap") is not None and ll.get("time")):
            return
        ms = _parse_ms(ll["time"])
        if ms is None:
            return
        lap = int(ll["lap"])
        self._last[num] = {"lap": lap, "ms": ms}
        if self._representative(num, lap) and self._update_fastest(lap, ms):
            self._recompute_all(clock_time)          # a same-lap fastest improved
        else:
            self._emit_colour(num, clock_time)

    def _update_fastest(self, lap: int, ms: int) -> bool:
        """Fold a representative lap-N time into that lap's fastest. True if it
        improved — the per-lap min only ever decreases, so the reference is stable
        and never drops when a car ahead clears its sectors."""
        cur = self._fastest_lap_ms.get(lap)
        if cur is None or ms < cur:
            self._fastest_lap_ms[lap] = ms
            return True
        return False

    def _recompute_all(self, clock_time: datetime) -> None:
        for num in list(self._last):
            self._emit_colour(num, clock_time)

    def _emit_colour(self, num: str, clock_time: datetime) -> None:
        last = self._last.get(num)
        if not last:
            return
        cls = self._cls.get(num, {}).get(last["lap"])
        st = self._status.get(num)
        if st == "DSQ":
            colour = "blank"     # disqualified → clear last-lap (client)
        elif st in ("RET", "STOP"):
            colour = "white"     # retired / stopped → dimmed white
        elif cls == "CHECKERED":
            # Post-chequered slow-down lap → shown dimmed white (matches white mini).
            colour = "white"
        else:
            # Race in/out laps keep DEFAULT band colours (vs the fastest same-lap),
            # NOT whited out — only P/Q whites in/out laps. (user 2026-07-08)
            ref = self._fastest_lap_ms.get(last["lap"])   # fastest on THIS lap number
            colour = "white" if ref is None else self._band(last["ms"] - ref)
        if self._colour.get(num) == colour:
            return
        self._colour[num] = colour
        self._bus.emit(f"driverPaceColour:{num}",
                       {"lap": last["lap"], "colour": colour}, clock_time)
