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
        self._sectors: dict[str, list] = {}
        # Max segment count seen per sector (track-wide — all cars share the
        # mini-sector layout). Mini-sector arrays are padded to this on every
        # emit so the layout is fixed-length and the client render is
        # width-invariant (no jitter as segments arrive within a lap).
        self._seg_counts: list[int] = [0, 0, 0]

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            sectors = self._sectors.setdefault(num, _empty_sectors())

            # Sector times are STICKY: a completed lap's sectors stay shown
            # until F1 clears them. At the new lap F1 sends Value="" per sector
            # to clear it (a same-time sector simply omits Value, so it keeps
            # showing). There is NO NoL-driven reset — the empty-Value clears
            # drive the rollover, so a same-time lap is still seen.
            changed = False
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
                            # Explicit clear (lap rollover) — reset this sector.
                            s["value"] = None
                            s["overallFastest"] = False
                            s["personalFastest"] = False
                            s["segments"] = []
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
                                s["segments"][si] = _segment_color(seg["Status"]); changed = True
            if changed:
                self._emit(num, clock_time)

    def _emit(self, num: str, clock_time: datetime) -> None:
        sec = self._sectors[num]
        self._bus.emit(f"driverSectors:{num}", [
            {"value": s["value"], "overallFastest": s["overallFastest"],
             "personalFastest": s["personalFastest"]} for s in sec
        ], clock_time)
        # Pad each sector's segments to the max count ever seen for that sector
        # so the array length (and thus the client layout) is fixed; trailing
        # not-yet-reached segments are None.
        mini = []
        for i, s in enumerate(sec):
            self._seg_counts[i] = max(self._seg_counts[i], len(s["segments"]))
            pad = self._seg_counts[i] - len(s["segments"])
            mini.append(list(s["segments"]) + [None] * pad)
        self._bus.emit(f"driverMiniSectors:{num}", mini, clock_time)
