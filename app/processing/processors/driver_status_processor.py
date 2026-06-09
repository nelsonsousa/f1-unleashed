"""
Driver Status Processor — single per-driver status string.

Subscribes to: TimingData, trackStatus (race), RaceControlMessages (practice/
               qualifying), qualifyingPart (qualifying)
Emits: driverStatus:{num}
    "DSQ" | "ELIMINATED" | "RET" | "STOP" | "OUT" | "PIT" | "FINISHED" | "TRACK"

Priority (highest wins):

    DSQ > ELIMINATED > RET > STOP > OUT > PIT > FINISHED > TRACK

DSQ is a latch set by a BLACK flag RCM (not black-and-white — that's a track-
limits warning); it outranks everything. ELIMINATED is the qualifying knockout
(KnockedOut) — terminal for the session. RET/STOP/OUT/PIT come from TimingData
booleans (Retired, Stopped, PitOut, InPit), each set when its field is True and
cleared when False. FINISHED is a latched flag set when the driver takes the
chequered flag — it sits BELOW PIT, so a driver who finishes and then pits
shows PIT. TRACK = no flag active (rendered as an empty badge).

Finished detection (moved here from the standings processor):
  - Race: the chequered (trackStatus "finished") marks the leader (P1) — they
    have just crossed S/F. Every other driver finishes as their next S/F
    crossing (NumberOfLaps increment) arrives.
  - Practice/qualifying: the "FIRST CAR TO TAKE THE FLAG - CAR N" RCM marks car
    N; everyone else finishes on their next S/F crossing or as they pit. Reset
    per qualifying segment (each Q part has its own chequered).
"""

import re
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# (F1 field, internal flag) in priority order — first active wins.
_FIELDS = (
    ("Retired", "retired"),
    ("Stopped", "stopped"),
    ("PitOut", "pitOut"),
    ("InPit", "inPit"),
)

# Practice/qualifying session end: the first car to cross under the chequered.
_FIRST_FLAG_RX = re.compile(r"FIRST CAR TO TAKE THE FLAG.*?CAR\s+(\d+)", re.I)
_CAR_RX = re.compile(r"CAR\s*(\d+)", re.I)


class DriverStatusProcessor(Processor):
    """Per-driver status: priority-max of the TimingData flags + FINISHED latch."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._is_qualifying = session_type == "qualifying"
        self._flags: dict[str, dict] = {}     # num -> {retired,stopped,pitOut,inPit}
        self._last: dict[str, str] = {}        # num -> last emitted status
        self._finished: dict[str, bool] = {}   # num -> taken the chequered
        self._knocked: set[str] = set()        # num -> eliminated (qualifying)
        self._dsq: set[str] = set()            # num -> black-flagged (disqualified)
        self._pos: dict[str, int] = {}         # num -> Position (race leader)
        self._nol: dict[str, int] = {}         # num -> NumberOfLaps (S/F crossings)
        # Chequered tracking.
        self._chequered = False
        self._lap_at_chequered: dict[str, int] = {}
        self._qualifying_part: Optional[int] = None

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle_timing)
        # RaceControlMessages: BLACK flag -> DSQ (all session types), plus the
        # practice/qualifying chequered ("FIRST CAR TO TAKE THE FLAG").
        self._bus.on("RaceControlMessages", self._handle_rcm)
        if self._is_race:
            self._bus.on("trackStatus", self._handle_track_status)
        if self._is_qualifying:
            self._bus.on("qualifyingPart", self._handle_qualifying_part)

    # ── Status emission ──
    def _status(self, num: str) -> str:
        if num in self._dsq:
            return "DSQ"
        if num in self._knocked:
            return "ELIMINATED"
        f = self._flags.get(num) or {}
        if f.get("retired"):
            return "RET"
        if f.get("stopped"):
            return "STOP"
        if f.get("pitOut"):
            return "OUT"
        if f.get("inPit"):
            return "PIT"
        if self._finished.get(num):
            return "FINISHED"
        return "TRACK"

    def _emit(self, num: str, clock_time: datetime) -> None:
        status = self._status(num)
        if self._last.get(num) != status:
            self._last[num] = status
            self._bus.emit(f"driverStatus:{num}", status, clock_time)

    # ── TimingData: flags + position + S/F crossings ──
    def _handle_timing(self, data: Any, clock_time: datetime) -> None:
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

            if "Position" in d:
                try:
                    self._pos[num] = int(d["Position"])
                except (TypeError, ValueError):
                    pass

            # Qualifying knockout — terminal for the session (ELIMINATED).
            if (self._is_qualifying and d.get("KnockedOut")
                    and num not in self._knocked):
                self._knocked.add(num)
                touched = True

            crossed = False
            if "NumberOfLaps" in d:
                try:
                    nl = int(d["NumberOfLaps"])
                    if nl > self._nol.get(num, 0):
                        self._nol[num] = nl
                        crossed = True
                except (TypeError, ValueError):
                    pass

            if self._maybe_finish(num, crossed):
                touched = True
            if touched:
                self._emit(num, clock_time)

    def _maybe_finish(self, num: str, crossed: bool) -> bool:
        """Flip a driver to finished once the flag has fallen. Returns True if
        the finished flag changed."""
        if not self._chequered or self._finished.get(num):
            return False
        # Already in the pits at/after the flag — they're done.
        if (self._flags.get(num) or {}).get("inPit"):
            self._finished[num] = True
            return True
        # Crossed S/F on the lap they were racing to complete.
        if crossed and num in self._lap_at_chequered:
            self._finished[num] = True
            return True
        return False

    # ── Race: chequered via track status ──
    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        if (not isinstance(data, dict) or data.get("status") != "finished"
                or self._chequered):
            return
        self._chequered = True
        running = sorted(
            (n for n, p in self._pos.items() if p < 99),
            key=lambda n: self._pos[n],
        )
        if not running:
            return
        leader = running[0]
        self._finished[leader] = True        # P1 just crossed S/F
        for n in running[1:]:
            self._lap_at_chequered[n] = self._nol.get(n, 0)
        for n in running:
            self._emit(n, clock_time)

    # ── RCM: BLACK-flag DSQ (all sessions) + P/Q chequered ──
    @staticmethod
    def _cars_in(msg: dict) -> list[str]:
        text = msg.get("Message", "") or ""
        cars = [m.group(1) for m in _CAR_RX.finditer(text)]
        if not cars and msg.get("RacingNumber") is not None:
            cars = [str(msg["RacingNumber"])]
        return cars

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        messages = data.get("Messages")
        if isinstance(messages, dict):
            items = list(messages.values())
        elif isinstance(messages, list):
            items = messages
        else:
            return
        dsq_changed: set[str] = set()
        for msg in items:
            if not isinstance(msg, dict):
                continue
            # BLACK flag (not "BLACK AND WHITE") = disqualification.
            if msg.get("Flag") == "BLACK":
                for n in self._cars_in(msg):
                    if n not in self._dsq:
                        self._dsq.add(n)
                        dsq_changed.add(n)
            # Practice/qualifying chequered — first car to take the flag.
            if not self._is_race and not self._chequered:
                m = _FIRST_FLAG_RX.search(msg.get("Message", "") or "")
                if m:
                    self._on_first_flag(m.group(1), clock_time)
        for n in dsq_changed:
            self._emit(n, clock_time)

    def _on_first_flag(self, num: str, clock_time: datetime) -> None:
        self._chequered = True
        self._finished[num] = True
        for n, f in self._flags.items():
            if n == num:
                continue
            self._lap_at_chequered[n] = self._nol.get(n, 0)
            if f.get("inPit"):
                self._finished[n] = True
        for n in set(self._flags) | {num}:
            self._emit(n, clock_time)

    # ── Qualifying: reset finished state per segment ──
    def _handle_qualifying_part(self, data: Any, clock_time: datetime) -> None:
        if not (isinstance(data, int) and 1 <= data <= 3):
            return
        if self._qualifying_part is None:
            self._qualifying_part = data
            return
        if data != self._qualifying_part:
            self._qualifying_part = data
            self._chequered = False
            self._lap_at_chequered = {}
            # Clear the chequered/finished latch for the new segment. The
            # eliminated keep showing ELIMINATED (KnockedOut persists, and
            # ELIMINATED outranks FINISHED), so wiping their finished flag
            # is harmless; advancing drivers go from FINISHED back to TRACK.
            affected = [n for n, v in self._finished.items() if v]
            self._finished = {}
            for n in affected:
                self._emit(n, clock_time)
