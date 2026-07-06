"""
Driver Gap Processor — gap column (+ race interval) per driver.

Subscribes to: TimingData
Emits:
  driverGap:{num}  {gap, cutoff, trend}   (race trend: green=shrinking, yellow=growing)
      race       : gap = GapToLeader,             cutoff False
      practice   : gap = TimeDiffToFastest (direct per-line field), cutoff False
      qualifying : cutoff = driver is in the elimination zone (POSITIONAL —
                   not KnockedOut and Position past the cutoff). cutoff is NOT
                   F1's per-driver Cutoff flag (verified unreliable — it doesn't
                   track the drop zone). Cutoff position is fixed by the rules
                   (top 16 advance from Q1, top 10 from Q2 — always, regardless
                   of how many cars actually entered):
                       Q1: 16   (P17+ eliminated)
                       Q2: 10   (P11+ eliminated)
                       Q3: no cutoff
                   cutoff False -> gap = gap to P1   (Stats.TimeDiffToFastest)
                   cutoff True  -> gap = gap to cutoff (driverBest - CutOffTime)
                   gap = "" when the driver has no best lap yet.

  driverInt:{num}  {interval, trend} — car ahead, RACE ONLY (IntervalToPositionAhead;
                   trend green=shrinking / yellow=growing vs previous value)

SessionPart / CutOffTime are top-level TimingData fields. Because a
driver can be bumped into/out of the zone by someone else's lap, the whole quali
field is recomputed whenever any zone-relevant input changes (positions, bests,
CutOffTime, SessionPart, KnockedOut); the per-driver emit dedups.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


def _fmt_gap(ms: int) -> str:
    return f"{'+' if ms >= 0 else '-'}{abs(ms) / 1000:.3f}"


def _secs(s: Any) -> Optional[float]:
    """Seconds from a gap/interval string ("+1.234", "+1:23.456", "-0.5").
    Returns None for non-numeric values ("+1 LAP", "LAP", "", placeholders)."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or "L" in s.upper():        # "1 LAP", "LAP", "1L" — lapped, not a time
        return None
    neg = s.startswith("-")
    body = s.lstrip("+-").strip()
    if not body:
        return None
    try:
        if ":" in body:
            mm, rest = body.split(":", 1)
            val = int(mm) * 60 + float(rest)
        else:
            val = float(body)
    except ValueError:
        return None
    return -val if neg else val


def _stats_timediff(stats: Any, key: str) -> Optional[str]:
    """Last non-empty `key` across the Stats array (quali gap fields).

    A blank value is NOT the same as a missing one: F1 sends an EMPTY
    TimeDiffToFastest when a driver takes P1, which means "clear the gap" — so
    return "" when the key is PRESENT but empty, and None only when it's ABSENT
    (no update). (card 3ODYNuKJ)"""
    entries = stats.values() if isinstance(stats, dict) else stats
    val = None
    present = False
    for e in entries:
        if isinstance(e, dict) and key in e:
            present = True
            if e[key]:
                val = e[key]
    if val is not None:
        return val
    return "" if present else None


# ── Race gap/int trend colours (card t46cHyov) ─────────────────────────────
# Validated on the Silverstone race data. GAP-to-leader is compared LAP-OVER-LAP
# at the S/F crossing — a sub-lap window is swamped by the ±0.15 s intra-lap
# wiggle (cars trading time through corners). INTERVAL uses a per-sample band ×
# direction state machine, only while < 1 s, with hysteresis so a car parked on a
# band edge doesn't flicker. Closing cools; opening warms.

def _gap_delta_colour(delta: float) -> str:
    """Lap-over-lap change in gap-to-leader → colour. Decreasing (catching the
    leader) is cool; increasing (dropping back) is warm."""
    if delta <= -1.0:  return "purple"
    if delta <= -0.5:  return "blue"
    if delta <= -0.25: return "green"
    if delta < 0.25:   return "white"
    if delta < 0.5:    return "yellow"
    if delta < 1.0:    return "orange"
    return "red"


_INT_BND = (0.25, 0.5, 1.0)     # band edges; band 0=<.25  1=.25-.5  2=.5-1  3=>1
_INT_HYST = 0.10                # must move this far past an edge to switch band
_INT_CLOSE = {0: "purple", 1: "blue", 2: "green"}    # entered a band by closing
_INT_OPEN = {0: "yellow", 1: "orange", 2: "red"}     # entered a band by opening
_WARM = frozenset(_INT_OPEN.values())
_INT_SETTLE = 2     # after this many consecutive warm samples → cool to the band


def _int_band(iv: float, cur: Optional[int]) -> int:
    if cur is None:
        return 3 if iv > 1.0 else 2 if iv >= 0.5 else 1 if iv >= 0.25 else 0
    b = cur
    while b < 3 and iv > _INT_BND[b] + _INT_HYST:
        b += 1
    while b > 0 and iv < _INT_BND[b - 1] - _INT_HYST:
        b -= 1
    return b


class DriverGapProcessor(Processor):
    """Per-driver gap column (P1 / cutoff) and race interval."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._is_qualifying = session_type == "qualifying"
        # quali state
        self._cutoff_time_ms: Optional[int] = None
        self._session_part: Optional[int] = None
        self._pos: dict[str, int] = {}
        self._knocked: dict[str, bool] = {}
        self._best_ms: dict[str, int] = {}
        self._gap_p1: dict[str, str] = {}
        self._seen: set[str] = set()
        # Eliminated drivers: frozen gap to the bubble (last advancing car) of the
        # part they went out in — captured once, kept + shown white thereafter.
        self._elim_gap: dict[str, str] = {}
        # emit dedup
        self._last_gap: dict[str, dict] = {}
        self._last_int: dict[str, Any] = {}
        # race gap/int trend (card t46cHyov).
        self._laps: dict[str, int] = {}
        self._race_pos: dict[str, int] = {}
        # gap trend: lap-over-lap Δ of gap-to-leader at the S/F crossing.
        self._gap_latest: dict[str, float] = {}    # continuous gap-to-leader (s)
        self._gap_at_lap: dict[str, float] = {}    # gap captured at the last crossing
        self._gap_trend: dict[str, str] = {}       # held colour between crossings
        # int trend: per-sample band × direction state machine (< 1 s only).
        self._int_band: dict[str, Optional[int]] = {}
        self._int_trend: dict[str, str] = {}
        self._int_pos: dict[str, Optional[int]] = {}
        self._int_hold: dict[str, int] = {}   # samples held on the current warm colour

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines")
        if not isinstance(lines, dict):
            lines = {}
        if self._is_qualifying:
            self._handle_quali(data, lines, clock_time)
        elif self._is_race:
            self._handle_race(lines, clock_time)
        else:
            self._handle_practice(lines, clock_time)

    def _emit_gap(self, num: str, gap: Optional[str], cutoff: bool,
                  clock_time: datetime, trend: str = "") -> None:
        payload = {"gap": gap if gap is not None else "", "cutoff": cutoff, "trend": trend}
        if payload != self._last_gap.get(num):
            self._last_gap[num] = payload
            self._bus.emit(f"driverGap:{num}", payload, clock_time)

    # ── Race ──
    def _handle_race(self, lines: dict, clock_time: datetime) -> None:
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "Position" in d:
                try:
                    self._race_pos[num] = int(d["Position"])
                except (TypeError, ValueError):
                    pass
            # Latest gap-to-leader first, so a crossing captures the S/F-line value.
            g = None
            if "GapToLeader" in d:
                g = d["GapToLeader"]
                gs = _secs(g)
                if gs is not None:
                    self._gap_latest[num] = gs
            # S/F crossing → recompute the LAP-OVER-LAP gap trend.
            if "NumberOfLaps" in d:
                try:
                    newlap = int(d["NumberOfLaps"])
                except (TypeError, ValueError):
                    newlap = None
                if newlap is not None:
                    old = self._laps.get(num)
                    self._laps[num] = newlap
                    if old is not None and newlap > old:
                        self._update_gap_trend(num, newlap)
            if g is not None:
                self._emit_gap(num, g, False, clock_time, self._gap_trend.get(num, ""))
            if "IntervalToPositionAhead" in d:
                v = d["IntervalToPositionAhead"]
                if isinstance(v, dict):
                    v = v.get("Value", "")
                trend = self._int_state(num, _secs(v), self._race_pos.get(num))
                payload = {"interval": v, "trend": trend}
                if payload != self._last_int.get(num):
                    self._last_int[num] = payload
                    self._bus.emit(f"driverInt:{num}", payload, clock_time)

    def _update_gap_trend(self, num: str, newlap: int) -> None:
        """Gap-to-leader change vs the previous S/F crossing → 7-colour trend.
        Ignores the first two laps (start chaos); needs a prior crossing value."""
        cur = self._gap_latest.get(num)
        prev = self._gap_at_lap.get(num)
        if cur is not None and prev is not None and newlap >= 3:
            self._gap_trend[num] = _gap_delta_colour(cur - prev)
        else:
            self._gap_trend[num] = ""
        if cur is not None:
            self._gap_at_lap[num] = cur

    def _int_state(self, num: str, iv: Optional[float], pos: Optional[int]) -> str:
        """Interval-to-car-ahead trend: band × direction, < 1 s only. Closing
        cools (green→blue→purple); opening warms (red→orange→yellow); a position
        switch flips the passed car to yellow. Hysteresis prevents edge flicker."""
        if iv is None:                        # lapped / non-numeric → out of battle
            self._int_band[num] = None
            self._int_trend[num] = ""
            self._int_pos[num] = pos
            self._int_hold[num] = 0
            return ""
        prev_b = self._int_band.get(num)
        nb = _int_band(iv, prev_b)
        prev_pos = self._int_pos.get(num)
        col = self._int_trend.get(num, "")
        if nb == 3:
            col = "white"; self._int_hold[num] = 0
        elif prev_pos is not None and pos is not None and pos > prev_pos:
            col = _INT_OPEN[nb]; self._int_hold[num] = 1   # passed → opening (warm #1)
        elif prev_b is None or prev_b == 3:
            col = _INT_CLOSE[nb]; self._int_hold[num] = 0  # entered the battle = closing
        elif nb != prev_b:
            if nb < prev_b:
                col = _INT_CLOSE[nb]; self._int_hold[num] = 0
            else:
                col = _INT_OPEN[nb]; self._int_hold[num] = 1   # opening (warm #1)
        elif col in _WARM:
            # Held on a warm (opening) band: after _INT_SETTLE consecutive warm
            # samples the car has settled at this gap — cool it to the band's
            # closing colour. (user refinement)
            h = self._int_hold.get(num, 1) + 1
            self._int_hold[num] = h
            if h > _INT_SETTLE:
                col = _INT_CLOSE[nb]
        # else: held on a cool band → hold the colour
        self._int_band[num] = nb
        self._int_trend[num] = col
        self._int_pos[num] = pos
        return col

    # ── Practice ── (TimeDiffToFastest is a direct per-line field, no Stats)
    def _handle_practice(self, lines: dict, clock_time: datetime) -> None:
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "TimeDiffToFastest" in d:
                self._emit_gap(num, d["TimeDiffToFastest"], False, clock_time)

    # ── Qualifying ── (positional elimination zone)
    def _cutoff_position(self) -> Optional[int]:
        # Fixed by the rules: top 16 advance from Q1, top 10 from Q2.
        if self._session_part == 1:
            return 16
        if self._session_part == 2:
            return 10
        return None    # Q3 / unknown — no cutoff

    def _handle_quali(self, data: dict, lines: dict, clock_time: datetime) -> None:
        changed = False
        if "CutOffTime" in data:
            ms = _parse_ms(data["CutOffTime"])
            if ms != self._cutoff_time_ms:
                self._cutoff_time_ms = ms; changed = True
        if "SessionPart" in data and data["SessionPart"] != self._session_part:
            self._session_part = data["SessionPart"]; changed = True
            # New part → clear each still-in driver's carried gap + best so the
            # gap column blanks (server-driven) until they set a lap in the new
            # part. Eliminated keep their frozen bubble gap (see _elim_gap).
            for n in list(self._seen):
                if not self._knocked.get(n):
                    self._gap_p1[n] = ""
                    self._best_ms.pop(n, None)

        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            self._seen.add(num)
            if "Position" in d:
                try:
                    p = int(d["Position"])
                    if p != self._pos.get(num):
                        self._pos[num] = p; changed = True
                except (TypeError, ValueError):
                    pass
            if "KnockedOut" in d:
                v = bool(d["KnockedOut"])
                if v != self._knocked.get(num):
                    self._knocked[num] = v; changed = True
            blt = d.get("BestLapTime")
            if isinstance(blt, dict) and blt.get("Value"):
                ms = _parse_ms(blt["Value"])
                if ms is not None and ms != self._best_ms.get(num):
                    self._best_ms[num] = ms; changed = True
            stats = d.get("Stats")
            if isinstance(stats, (list, dict)):
                g = _stats_timediff(stats, "TimeDiffToFastest")
                if g is not None and g != self._gap_p1.get(num):
                    self._gap_p1[num] = g; changed = True

        if not changed:
            return
        # Recompute the whole field — one driver's lap can flip another's zone.
        cutoff_pos = self._cutoff_position()
        # Best lap of the car in the last ADVANCING position (P16 in Q1 /
        # P10 in Q2). KO-zone drivers' gap is measured to it — how far they
        # are from safety. (F1's CutOffTime = 107% of P1 is not meaningful
        # for this and is no longer used for the gap value.)
        cutoff_best_ms = None
        if cutoff_pos is not None:
            for n, p in self._pos.items():
                if p == cutoff_pos:
                    cutoff_best_ms = self._best_ms.get(n)
                    break
        for num in self._seen:
            if self._knocked.get(num):
                # Eliminated: freeze the gap to the bubble (last advancing car) of
                # the part they went out in — their last in-zone gap already IS the
                # gap to the cutoff car — and show it WHITE (cutoff=False), not red
                # (cards 1smo53RX / US3eJeKz).
                if num not in self._elim_gap:
                    prev = (self._last_gap.get(num) or {}).get("gap")
                    if prev:
                        self._elim_gap[num] = prev
                    else:
                        bms = self._best_ms.get(num)
                        self._elim_gap[num] = (_fmt_gap(bms - cutoff_best_ms)
                                               if bms is not None and cutoff_best_ms is not None else "")
                self._emit_gap(num, self._elim_gap[num], False, clock_time)
                continue
            in_zone = (cutoff_pos is not None
                       and self._pos.get(num, 0) > cutoff_pos)
            if self._pos.get(num) == 1:
                # P1 is the fastest — no gap to itself. Belt-and-braces alongside
                # the _stats_timediff blank-vs-missing fix: also clears the gap the
                # instant a driver takes P1 (the Position update can arrive a
                # message before the empty TimeDiffToFastest). (card 3ODYNuKJ)
                gap = ""
            elif in_zone:
                bms = self._best_ms.get(num)
                gap = (_fmt_gap(bms - cutoff_best_ms)
                       if bms is not None and cutoff_best_ms is not None else "")
            else:
                gap = self._gap_p1.get(num, "")
            self._emit_gap(num, gap, in_zone, clock_time)
