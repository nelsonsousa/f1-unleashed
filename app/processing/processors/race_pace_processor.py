"""
Race Pace Processor — colour-codes each driver's last lap time against the race
leader's pace (RACE ONLY).  Trello: "Current race pace".

Subscribes (wildcard) to: driverLaps:{num}, driverLapClassification:{num}, standings
Emits: driverPaceColour:{num}  { lap, colour }   (persisted)

Reference = the race leader's (P1) last REPRESENTATIVE lap time, i.e. excluding
in-laps (PIT), out-laps (OUT) and STOP. If the leader's most recent lap is one of
those, the reference is left unchanged. Each driver's last representative lap is
then compared to the reference and binned (delta = driver − reference):

    delta <= 0           purple   (faster than or equal to the leader)
    +0.001 .. +0.250     blue
    +0.251 .. +0.500     green
    +0.501 .. +1.000     yellow
    +1.001 .. +2.000     orange
    > +2.000             red

A driver whose last lap is an in/out/stop lap is WHITE — an in/out lap isn't a
representative racing lap (2026-07 SME rule; supersedes qKVcxF9n's keep-the-band-
through-the-pit). "white" is also used before any reference exists. The whole field
is recomputed whenever the leader posts a new representative lap (or the leader
changes).
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
        self._leader: Optional[str] = None
        self._ref_ms: Optional[int] = None          # leader's last representative lap time
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
        elif topic == "standings":
            self._on_standings(data, clock_time)
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
        # If this updates the representativeness of the driver's last completed
        # lap, refresh (their colour, and the reference if they're the leader).
        last = self._last.get(num)
        if last and last["lap"] == lap:
            if num == self._leader:
                self._refresh_reference(clock_time)
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
        self._last[num] = {"lap": int(ll["lap"]), "ms": ms}
        if num == self._leader:
            self._refresh_reference(clock_time)
        else:
            self._emit_colour(num, clock_time)

    def _on_standings(self, data: Any, clock_time: datetime) -> None:
        drivers = data.get("drivers") if isinstance(data, dict) else None
        leader = drivers[0]["num"] if drivers else None
        if leader == self._leader:
            return
        self._leader = leader
        # Adopt the new leader's last representative lap as the reference.
        last = self._last.get(leader) if leader else None
        if last and self._representative(leader, last["lap"]):
            self._ref_ms = last["ms"]
        self._recompute_all(clock_time)

    def _refresh_reference(self, clock_time: datetime) -> None:
        """Leader posted a new lap — update the reference iff it's representative,
        then recompute the field (or just the leader if the reference is unchanged)."""
        last = self._last.get(self._leader) if self._leader else None
        new_ref = self._ref_ms
        if last and self._representative(self._leader, last["lap"]):
            new_ref = last["ms"]
        if new_ref != self._ref_ms:
            self._ref_ms = new_ref
            self._recompute_all(clock_time)
        elif self._leader is not None:
            self._emit_colour(self._leader, clock_time)

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
            # The finishing lap is a normal racing lap, so it bands below. (user)
            colour = "white"
        elif self._ref_ms is None:
            colour = "white"
        elif not self._representative(num, last["lap"]):
            # In/out/stop lap → WHITE (not a representative racing lap; supersedes
            # qKVcxF9n's "keep the band through the pit").
            colour = "white"
        else:
            colour = self._band(last["ms"] - self._ref_ms)
        if self._colour.get(num) == colour:
            return
        self._colour[num] = colour
        self._bus.emit(f"driverPaceColour:{num}",
                       {"lap": last["lap"], "colour": colour}, clock_time)
