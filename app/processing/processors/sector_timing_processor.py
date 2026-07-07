"""
Sector Timing Processor — per-driver current-lap sector times + mini-sectors.

Subscribes to: TimingData
Emits:
  driverSectors:{num}      [ {value, overallFastest, personalFastest} x3 ]
  driverMiniSectors:{num}  [ [segment-colour|None, ...] x3 ]  (the segment dots)

Sector times are STICKY (F1 deltas): a completed lap's sectors keep showing
until F1 clears them with per-sector Value="" at the new lap (a same-time sector
omits Value, so it stays visible). No NoL-driven reset. Split out of the old
timing_processor's bundled driverTiming topic.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# F1 segment Status code -> colour (None = no data yet).
SEGMENT_COLORS = {
    0: None,
    2048: "#ffd700",   # yellow  — completed, no improvement
    2049: "#00ff00",   # green   — personal best
    2050: "#00ff00",
    2051: "#ff00ff",   # purple  — overall best
    2052: "#ff00ff",
    2064: "#ffffff",   # white   — out / slow
}


def _segment_color(status: Any) -> Optional[str]:
    try:
        return SEGMENT_COLORS.get(int(status))
    except (TypeError, ValueError):
        return None


def _empty_sectors() -> list:
    return [{"value": None, "overallFastest": False, "personalFastest": False, "segments": []}
            for _ in range(3)]


class SectorTimingProcessor(Processor):
    """Per-driver sector times + mini-sector segment colours for the current lap."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._sectors: dict[str, list] = {}
        self._status: dict[str, Optional[str]] = {}   # num -> driverStatus
        self._cls_lap: dict[str, dict] = {}            # num -> {lap -> classification type}
        self._current_lap: dict[str, Any] = {}         # num -> driverLaps.currentLap
        self._display_lap: dict[str, Any] = {}         # num -> lap the SHOWN sector TIMES belong to
        self._mini_display_lap: dict[str, Any] = {}    # num -> lap the SHOWN mini-sectors belong to
        self._part: Optional[int] = None   # current qualifying part (reset trigger)
        # Max segment count seen per sector (track-wide — all cars share the
        # mini-sector layout). Mini-sector arrays are padded to this on every
        # emit so the layout is fixed-length and the client render is
        # width-invariant (no jitter as segments arrive within a lap).
        self._seg_counts: list[int] = [0, 0, 0]

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)
        self._bus.on("qualifyingPart", self._handle_part)
        self._bus.on("*", self._handle_aux)   # driverStatus / driverLapClassification

    def _handle_aux(self, topic: str, data: Any, clock_time: datetime) -> None:
        """Status / lap-classification drive the display suppression (retired,
        eliminated, finished, out/in/slow). Not a cycle — nothing in their
        producers consumes driverSectors."""
        if topic.startswith("driverStatus:"):
            num = topic.split(":", 1)[1]
            st = data if isinstance(data, str) else None
            if st != self._status.get(num):
                self._status[num] = st
                if num in self._sectors:
                    self._emit(num, clock_time)
        elif topic.startswith("driverLapClassification:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict):
                lap = data.get("lap")
                if isinstance(lap, int) and self._cls_lap.setdefault(num, {}).get(lap) != data.get("type"):
                    self._cls_lap[num][lap] = data.get("type")
                    if num in self._sectors:
                        self._emit(num, clock_time)
        elif topic.startswith("driverLaps:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict) and data.get("currentLap") is not None:
                self._current_lap[num] = data["currentLap"]

    def _suppress_mode(self, num: str):
        """(sector_mode, mini_mode), each None | 'white' | 'blank'. Sector times and
        mini-sectors share the SAME suppression: 'white' shows the value/segments
        dimmed white (0.5 opacity), 'blank' clears them. Sector TIMES use
        _display_lap (sticky until the rollover Value="" clear); MINI-SECTORS use
        _mini_display_lap — they paint from the lap START, before that clear, so
        keying them off _display_lap left a whole first sector on the PREVIOUS lap's
        class. (user 2026-07-07)"""
        st = self._status.get(num)
        if st in ("DSQ", "ELIMINATED"):
            return ("blank", "blank")                 # out of contention → clear both
        if st in ("RET", "STOP"):
            return ("white", "white")                 # retired / stopped → dimmed white
        sector_mode, _ = self._suppresses(num, self._display_lap.get(num))
        _, mini_mode = self._suppresses(num, self._mini_display_lap.get(num))
        return (sector_mode, mini_mode)

    def _suppresses(self, num: str, lap: Any):
        """(sector_mode, mini_mode) for the classification of `lap`. Every
        non-representative lap — out / in / cool-down / post-chequered — shows dimmed
        white (not cleared). The post-chequered slow-down lap is classified SLOW, so
        this covers FINISHED too: the finishing lap keeps its class, the slow-down
        lap is SLOW."""
        dcls = self._cls_lap.get(num, {}).get(lap)
        if dcls in ("OUT", "PIT", "SLOW", "CHECKERED"):
            return ("white", "white")                 # out / in / cool-down / post-flag → dimmed white
        return (None, None)

    def _handle_part(self, data: Any, clock_time: datetime) -> None:
        """New qualifying part → blank every driver's sector times + mini-sectors
        so the client shows a clean slate, server-driven (no client-side reset)."""
        part = data if isinstance(data, int) else None
        if part is None or part == self._part:
            return
        self._part = part
        for num in list(self._sectors.keys()):
            self._sectors[num] = _empty_sectors()
            self._emit(num, clock_time)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            sectors = self._sectors.setdefault(num, _empty_sectors())
            if self._display_lap.get(num) is None:
                self._display_lap[num] = self._current_lap.get(num)
            if self._mini_display_lap.get(num) is None:
                self._mini_display_lap[num] = self._current_lap.get(num)

            # Sector times are STICKY: a completed lap's sectors stay shown
            # until F1 clears them. At the new lap F1 sends Value="" per sector
            # to clear it (a same-time sector simply omits Value, so it keeps
            # showing). There is NO NoL-driven reset — the empty-Value clears
            # drive the rollover, so a same-time lap is still seen.
            changed = False
            seg_painted = False
            sp = d.get("Sectors")
            if sp:
                items = sp.items() if isinstance(sp, dict) else enumerate(sp)
                for idx, sec in items:
                    if not isinstance(sec, dict):
                        continue
                    i = int(idx)
                    if i >= 3:
                        continue
                    s = sectors[i]
                    if "Value" in sec:
                        if sec["Value"]:
                            s["value"] = sec["Value"]
                        else:
                            # Explicit clear (lap rollover) — reset this sector and
                            # advance the DISPLAY lap to the current lap: the shown
                            # sectors now belong to the new lap (the boundary S3/mini
                            # before this belonged to the previous lap). (user)
                            s["value"] = None
                            s["overallFastest"] = False
                            s["personalFastest"] = False
                            s["segments"] = []
                            self._display_lap[num] = self._current_lap.get(num)
                        changed = True
                    if "OverallFastest" in sec:
                        s["overallFastest"] = bool(sec["OverallFastest"]); changed = True
                    if "PersonalFastest" in sec:
                        s["personalFastest"] = bool(sec["PersonalFastest"]); changed = True
                    segp = sec.get("Segments")
                    if segp:
                        seg_items = segp.items() if isinstance(segp, dict) else enumerate(segp)
                        for sidx, seg in seg_items:
                            if not isinstance(seg, dict):
                                continue
                            si = int(sidx)
                            while len(s["segments"]) <= si:
                                s["segments"].append(None)
                            if "Status" in seg:
                                s["segments"][si] = _segment_color(seg["Status"])
                                changed = True; seg_painted = True
            # Mini-sectors paint from the lap START: advance _mini_display_lap on the
            # FIRST segment message after the lap increment, so the new lap's segments
            # take its own class immediately — instead of the previous lap's, which
            # _display_lap only sheds at the far-later Value="" clear. (user)
            if seg_painted and self._mini_display_lap.get(num) != self._current_lap.get(num):
                self._mini_display_lap[num] = self._current_lap.get(num)
            if changed:
                self._emit(num, clock_time)

    def _emit(self, num: str, clock_time: datetime) -> None:
        sec = self._sectors[num]
        sector_mode, mini_mode = self._suppress_mode(num)
        if sector_mode == "blank":
            payload = [{"value": None, "overallFastest": False, "personalFastest": False, "white": False}
                       for _ in range(3)]
        else:
            white = sector_mode == "white"            # slow / out / post-flag → value shown dimmed white
            payload = [{"value": s["value"], "overallFastest": s["overallFastest"],
                        "personalFastest": s["personalFastest"], "white": white} for s in sec]
        self._bus.emit(f"driverSectors:{num}", payload, clock_time)
        # Pad each sector's segments to the max count ever seen for that sector
        # so the array length (and thus the client layout) is fixed; trailing
        # not-yet-reached segments are None. Suppression: white = all segments
        # white, blank = all empty.
        mini = []
        for i, s in enumerate(sec):
            self._seg_counts[i] = max(self._seg_counts[i], len(s["segments"]))
            cnt = self._seg_counts[i]
            if mini_mode == "white":
                # Repaint PAINTED segments white; null stays null — keeps the
                # painted-vs-null progress shape (where the driver is on track).
                mini.append(["#ffffff" if seg else None for seg in s["segments"]]
                            + [None] * (cnt - len(s["segments"])))
            elif mini_mode == "blank":
                mini.append([None] * cnt)
            else:
                mini.append(list(s["segments"]) + [None] * (cnt - len(s["segments"])))
        self._bus.emit(f"driverMiniSectors:{num}", mini, clock_time)
