"""Live in-race pit-stop time-loss measurement (races only) — deterministic int-chain algorithm.

Per pit stop by driver A (SME-directed 2026-07-13):
  T1 = t(status=PIT) − 10 s   (before the pre-entry position churn)
  T2 = when A's gap/int is re-reported after status=TRACK (rejoin, already settled)
  Cohort = drivers in A's INT-CHAIN at T1 that are STILL in A's int-chain at T2, excluding any
    that were in the pit (status PIT/OUT) at any point in [T1,T2] and any glitch cars (no gap+int).
  Reference axis: if A is on the leader's lap at both T1 and T2 → GAP-to-leader (A↔X gap =
    gap(A)−gap(X), so a changing leader cancels); else the INT chain (blank int = 0). A one-member
    chain (a car >1 lap clear of everyone) has no references → blank.
  Per cohort driver X: loss_X = (gap A↔X at T2) − (gap A↔X at T1). Outliers dropped.
  Time lost = average of loss vs the CLOSEST non-eliminated car ahead and behind A at T1; if A is
    first/last of the chain, the two closest on the available side; 1 left → that one; 0 → blank.

Emitted as `pitStopTimeLoss` (full state each stop) — a per-stop list for the "Pit stops" tab and
a running observed estimate. SC/VSC are NOT special-cased: pairwise gaps to same-chain non-pitting
cars are frame-independent, so they measure the (cheaper) SC/VSC loss directly.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import deque
from datetime import datetime
from typing import Any, Optional

from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)

PRE_ENTRY_MS = 10000       # T1 = InPit − 10 s
SNAP_THROTTLE_MS = 1000    # min spacing between field snapshots
_CLS_RANK = {"green": 0, "vsc": 1, "sc": 2}
# "N L"/"NL" = N laps down (Int break / lapped gap); "LAP N" = the leader's lap indicator (NOT lapped).
_LAPPED_RE = re.compile(r"^\d+\s*L\b", re.I)


def _reduce_status(s: str) -> str:
    return s if s in ("sc", "vsc") else "green"


def _secs(v: Any) -> Optional[float]:
    """'+1.234' → 1.234 s; leader/lapped/blank → None."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or "L" in s.upper():
        return None
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return None


class PitStopLossProcessor(Processor):
    def __init__(self, bus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._order: list[str] = []                           # standings order (leader first)
        self._gap: dict[str, Optional[float]] = {}            # last-known numeric gap-to-leader (s)
        self._gap_lapped: dict[str, bool] = {}                # gap reads "N L" (A is lapped)
        self._int: dict[str, float] = {}                      # current interval to car ahead; blank=0
        self._int_break: dict[str, bool] = {}                 # int reads "N L" — breaks the chain here
        self._status: dict[str, str] = {}
        self._lap: dict[str, Optional[int]] = {}
        self._tyre: dict[str, str] = {}
        self._drivers: dict[str, dict] = {}
        self._track_status = "green"
        self._race_started = False
        self._snaps: deque = deque(maxlen=600)                # (off, order, statedict)
        self._last_snap = -SNAP_THROTTLE_MS
        self._windows: dict[str, list] = {}                   # num -> [[t_pit, t_track_or_None], …]
        self._speed_hist: dict[str, deque] = {}               # standstill capture while in the pit
        self._f1_lane: dict[str, list] = {}
        self._f1_stat: dict[str, list] = {}
        self._stop_count: dict[str, int] = {}
        self._pending: list[dict] = []
        self._stops: list[dict] = []

    def subscribe(self) -> None:
        if not self._is_race:
            return
        self._bus.on("*", self._handle_wildcard)
        self._bus.on("standings", self._handle_standings)
        self._bus.on("driverList", self._handle_driver_list)
        self._bus.on("trackStatus", self._handle_track_status)
        self._bus.on("PitLaneTimeCollection", self._handle_pit_lane_time)
        self._bus.on("PitStop", self._handle_pit_stop)
        self._bus.on("PitStopSeries", self._handle_pit_stop_series)

    # ── inputs ──────────────────────────────────────────────────────────────
    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        off = _off(clock_time)
        if topic.startswith("driverGap:"):
            num = topic.split(":", 1)[1]
            raw = data.get("gap") if isinstance(data, dict) else None
            g = _secs(raw)
            if g is not None:
                self._gap[num] = g
                self._gap_lapped[num] = False
            elif isinstance(raw, str) and _LAPPED_RE.match(raw.strip()):
                self._gap_lapped[num] = True                  # lapped; keep last gap but flag it
            self._snap(off)
            self._maybe_close(num, off, clock_time)
        elif topic.startswith("driverInt:"):
            num = topic.split(":", 1)[1]
            raw = data.get("interval") if isinstance(data, dict) else None
            iv = _secs(raw)
            if iv is not None:
                self._int[num], self._int_break[num] = iv, False
            else:
                self._int[num] = 0.0                          # blank = 0
                self._int_break[num] = bool(isinstance(raw, str) and _LAPPED_RE.match(raw.strip()))
            self._snap(off)
            self._maybe_close(num, off, clock_time)
        elif topic.startswith("driverStatus:"):
            self._handle_status(topic.split(":", 1)[1], data, off, clock_time)
        elif topic.startswith("driverLaps:"):
            if isinstance(data, dict) and data.get("currentLap") is not None:
                self._lap[topic.split(":", 1)[1]] = data["currentLap"]
        elif topic.startswith("currentTyre:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict) and data.get("compound"):
                self._tyre[num] = data["compound"]   # compound fitted (set as the new stint opens)
        elif topic.startswith("liveTelemetry:"):
            num = topic.split(":", 1)[1]
            if num in self._speed_hist and isinstance(data, dict):
                self._speed_hist[num].append((off, data.get("speed")))

    def _handle_standings(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict) and isinstance(data.get("drivers"), list):
            self._order = [d["num"] for d in data["drivers"] if isinstance(d, dict) and "num" in d]

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            self._drivers = data

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict) and data.get("status"):
            self._track_status = data["status"]
            if data["status"] == "green":
                self._race_started = True
            red = _reduce_status(data["status"])
            if _CLS_RANK[red] > 0:
                for st in self._pending:
                    if st["t_track"] is None and _CLS_RANK[red] > _CLS_RANK.get(st["cls"], 0):
                        st["cls"] = red

    def _handle_pit_lane_time(self, data: Any, clock_time: datetime) -> None:
        for num, e in ((data or {}).get("PitTimes") or {}).items():
            if isinstance(e, dict) and e.get("Duration") is not None:
                self._f1_lane.setdefault(str(num), []).append((_int_(e.get("Lap")), _flt(e["Duration"])))

    def _handle_pit_stop(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            self._record_pit_stop(data)

    def _handle_pit_stop_series(self, data: Any, clock_time: datetime) -> None:
        for _n, arr in ((data or {}).get("PitTimes") or {}).items():
            for e in (arr if isinstance(arr, list) else [arr]):
                if isinstance(e, dict):
                    self._record_pit_stop(e.get("PitStop", {}))

    def _record_pit_stop(self, ps: dict) -> None:
        num = str(ps.get("RacingNumber") or "")
        if not num:
            return
        lane, stat, lap = _flt(ps.get("PitLaneTime")), _flt(ps.get("PitStopTime")), _int_(ps.get("Lap"))
        if lane is not None:
            self._f1_lane.setdefault(num, []).append((lap, lane))
        if stat is not None:
            self._f1_stat.setdefault(num, []).append((lap, stat))

    # ── field snapshots + int-chains ────────────────────────────────────────
    def _state(self) -> dict:
        """Per-car field state now: {num: (gap, gap_lapped, int, int_break, status)}; leader gap=0."""
        st = {}
        for i, num in enumerate(self._order):
            st[num] = (0.0 if i == 0 else self._gap.get(num),
                       False if i == 0 else bool(self._gap_lapped.get(num)),
                       self._int.get(num, 0.0), bool(self._int_break.get(num)),
                       self._status.get(num, ""))
        return st

    def _snap(self, off: int) -> None:
        if off - self._last_snap >= SNAP_THROTTLE_MS and self._order:
            self._snaps.append((off, list(self._order), self._state()))
            self._last_snap = off

    def _snap_at(self, ts: int):
        best = None
        for o, order, state in self._snaps:
            if o <= ts:
                best = (order, state)
            else:
                break
        return best if best is not None else (
            (self._snaps[0][1], self._snaps[0][2]) if self._snaps else (None, None))

    @staticmethod
    def _chain(order: list, state: dict, num: str) -> list:
        """The int-chain (contiguous same-lap run) containing `num`. A car whose Int reads 'N L'
        (int_break) starts a new chain."""
        if not order or num not in order:
            return []
        idx = order.index(num)

        def brk(i):
            s = state.get(order[i])
            return bool(s[3]) if s else False
        start = idx
        while start > 0 and not brk(start):
            start -= 1
        end = idx + 1
        while end < len(order) and not brk(end):
            end += 1
        return order[start:end]

    # ── pit-stop lifecycle ──────────────────────────────────────────────────
    def _handle_status(self, num: str, value: Any, off: int, clock_time: datetime) -> None:
        if not isinstance(value, str):
            return
        prev = self._status.get(num)
        self._status[num] = value
        if value == "PIT" and prev != "PIT" and self._race_started:
            self._windows.setdefault(num, []).append([off, None])
            self._speed_hist[num] = deque(maxlen=400)
            o1, s1 = self._snap_at(off - PRE_ENTRY_MS)
            self._pending.append({
                "num": num, "t_pit": off, "t_pit_utc": clock_time.isoformat(),
                "lap": self._lap.get(num), "cls": _reduce_status(self._track_status),
                "pos_before": self._pos(num), "order1": o1, "state1": s1,
                "t_track": None, "closed": False,
            })
        elif value == "TRACK":                                 # rejoin — back on track
            for w in reversed(self._windows.get(num, [])):
                if w[1] is None:
                    w[1] = off
                    break
            for st in self._pending:
                if st["num"] == num and st["t_track"] is None:
                    st["t_track"] = off
                    break

    def _maybe_close(self, num: str, off: int, clock_time: datetime) -> None:
        """Close when A's gap/int is re-reported after TRACK (settled)."""
        for st in self._pending:
            if st["num"] == num and not st["closed"] and st["t_track"] is not None \
                    and off > st["t_track"]:
                st["closed"] = True
                self._finish(st, off, clock_time)

    def _finish(self, st: dict, off: int, clock_time: datetime) -> None:
        num = st["num"]
        time_lost = self._compute_loss(st, off)
        if time_lost is not None and not (0.0 <= time_lost <= 90.0):
            time_lost = None
        self._stops.append(self._build_stop(st, num, time_lost))
        self._speed_hist.pop(num, None)
        self._emit(clock_time)

    def _compute_loss(self, st: dict, off: int) -> Optional[float]:
        """Time lost = the loss vs ONE reference driver: the closest surviving cohort car AHEAD of X.

        The car ahead is X's own settling Int on rejoin, which updates as soon as X crosses the line —
        a follower's Int to X lags 5–10 s (it needs the follower to re-cross), so it's never used.
        The one exception: the race leader who pits and comes out STILL P1 has no car ahead, so he
        uses the closest surviving car behind. A leader who DROPS out of the lead uses the closest
        survivor now ahead of him. Single pairwise gap from RAW data at T1 & T2 — never an average,
        never a reconstructed axis — so chaos elsewhere in the order can't corrupt it. Each pair's gap
        is gap(X)−gap(R) when both have a numeric gap, else the Int sum walked between them (None if a
        lap boundary sits in between). Loss vs R = pairGap(T2) − pairGap(T1)."""
        X = st["num"]
        order1, state1 = st["order1"], st["state1"]
        order2, state2 = list(self._order), self._state()
        if not order1 or not state1 or X not in order1 or X not in order2:
            return None
        T1, T2 = st["t_pit"] - PRE_ENTRY_MS, off
        chain1 = self._chain(order1, state1, X)
        # cohort = X's int-chain at T1, minus X, co-pitters, glitch cars (no gap AND no int at T1),
        # and any car that has since dropped out of the order at T2.
        cohort = [x for x in chain1
                  if x != X and x in state1 and x in order2
                  and not self._pitted_between(x, T1, T2)
                  and (state1[x][0] is not None or state1[x][2] is not None)]
        if not cohort:
            return None
        idxX = order1.index(X)
        was_leader = idxX == 0
        still_leader = bool(order2) and order2[0] == X
        if was_leader and still_leader:                          # no car ahead → closest behind
            cands = sorted((x for x in cohort), key=lambda x: order1.index(x))
        elif was_leader:                                         # dropped the lead → survivor now ahead
            cands = sorted((x for x in cohort if order2.index(x) < order2.index(X)),
                           key=lambda x: order2.index(x), reverse=True)
        else:                                                    # closest surviving car ahead at T1
            cands = sorted((x for x in cohort if order1.index(x) < idxX),
                           key=lambda x: order1.index(x), reverse=True)
        return self._neighbour_loss(cands, order1, state1, order2, state2, X)

    def _neighbour_loss(self, cands, o1, s1, o2, s2, X) -> Optional[float]:
        """Closest candidate (already ordered nearest-first) for which the pair gap is computable at
        BOTH snapshots → its loss (pairGap@T2 − pairGap@T1). Skips any whose gap can't be built."""
        for R in cands:
            g1 = self._pair_gap(o1, s1, X, R)
            g2 = self._pair_gap(o2, s2, X, R)
            if g1 is not None and g2 is not None:
                return g2 - g1
        return None

    def _pair_gap(self, order, state, X, R) -> Optional[float]:
        """Signed seconds X is BEHIND R (positive = X further back). gap(X)−gap(R) when BOTH have a
        NUMERIC gap-to-leader (a current 'N L' reading is non-numeric — not usable for distances);
        else the Int sum walked along the order between them (None if a lap boundary — an Int reading
        'N L' — sits in the walk, i.e. gaps can't be built from Ints)."""
        if X not in order or R not in order:
            return None
        sx, sr = state.get(X), state.get(R)
        gx = sx[0] if (sx and not sx[1]) else None      # sx[1] = currently lapped ('N L') → non-numeric
        gr = sr[0] if (sr and not sr[1]) else None
        if gx is not None and gr is not None:
            return gx - gr
        iX, iR = order.index(X), order.index(R)
        if iR < iX:                                          # R ahead of X → X is +behind R
            s = self._sum_ints(order, state, iR + 1, iX)
            return s
        s = self._sum_ints(order, state, iX + 1, iR)         # R behind X → X is −(that) behind R
        return None if s is None else -s

    @staticmethod
    def _sum_ints(order, state, a, b) -> Optional[float]:
        """Sum of Ints for order positions a..b inclusive (each = interval to the car ahead), i.e. the
        time gap between positions a−1 and b. None if any car in the span is a lap down (int_break) —
        the span crosses a lap boundary and can't be summed as seconds. Blank int already stored as 0."""
        total = 0.0
        for i in range(a, b + 1):
            s = state.get(order[i])
            if s is None or s[3]:
                return None
            total += s[2]
        return total

    def _pitted_between(self, num: str, t1: int, t2: int) -> bool:
        for wp, wt in self._windows.get(num, []):
            if wp <= t2 and (wt if wt is not None else t2) >= t1:
                return True
        return False

    # ── stop dict / stationary ──────────────────────────────────────────────
    def _build_stop(self, st: dict, num: str, time_lost: Optional[float]) -> dict:
        stationary = self._stationary(num, st)
        info = self._drivers.get(num, {})
        self._stop_count[num] = self._stop_count.get(num, 0) + 1
        pos = self._pos(num)
        ahead = self._order[pos - 2] if (pos and pos >= 2) else None
        return {
            "num": num, "tla": info.get("tla", num), "color": info.get("color", "#888"),
            "stopNumber": self._stop_count[num], "t_pit_utc": st.get("t_pit_utc"),
            "posBefore": st.get("pos_before"), "posAfter": pos,
            "aheadStops": self._stop_count.get(ahead, 0) if ahead else None,
            # null when lapped ('N L') — a non-numeric interval can't drive numeric traffic logic.
            "intAfter_s": None if self._int_break.get(num) else self._int.get(num),
            "compound": self._tyre.get(num),
            "lap": st["lap"], "cls": st["cls"],
            "timeLost_s": round(time_lost, 1) if time_lost is not None else None,
            "stationary_s": round(stationary, 1) if stationary is not None else None,
            "pitLaneTime_s": self._f1_pick(self._f1_lane, num, st["lap"]),
        }

    def _stationary(self, num: str, st: dict) -> Optional[float]:
        f1 = self._f1_pick(self._f1_stat, num, st["lap"])
        if f1 is not None:
            return f1
        best = rs = re = None
        for o, s in (self._speed_hist.get(num) or []):
            if isinstance(s, (int, float)) and s <= 5.0:
                if rs is None:
                    rs = o
                re = o
            elif rs is not None:
                best = max(best or 0, re - rs)
                rs = None
        if rs is not None:
            best = max(best or 0, re - rs)
        return best / 1000.0 if best else None

    @staticmethod
    def _f1_pick(table: dict, num: str, lap: Optional[int]) -> Optional[float]:
        rows = table.get(num)
        if not rows:
            return None
        if lap is not None:
            for lp, val in rows:
                if lp == lap:
                    return val
        return rows[-1][1]

    def _pos(self, num: str) -> Optional[int]:
        return self._order.index(num) + 1 if num in self._order else None

    # ── output ──────────────────────────────────────────────────────────────
    def _emit(self, clock_time: datetime) -> None:
        def med(cls):
            vals = _iqr_keep([s["timeLost_s"] for s in self._stops
                              if s["cls"] == cls and s["timeLost_s"] is not None])
            return round(statistics.median(vals), 1) if vals else None
        transits = _iqr_keep([s["pitLaneTime_s"] - s["stationary_s"] for s in self._stops
                              if s.get("pitLaneTime_s") is not None and s.get("stationary_s") is not None])
        observed = {
            "green_s": med("green"), "vsc_s": med("vsc"), "sc_s": med("sc"),
            "transit_s": round(statistics.mean(transits), 1) if transits else None,
            "transit_std_s": (round(statistics.stdev(transits), 1) if len(transits) > 1 else 0.0)
            if transits else None,
        }
        self._bus.emit("pitStopTimeLoss", {"stops": self._stops, "observed": observed}, clock_time)


# ── module helpers ───────────────────────────────────────────────────────────
def _off(clock_time: datetime) -> int:
    return int(clock_time.timestamp() * 1000)


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iqr_keep(xs):
    xs2 = sorted(x for x in xs if x is not None)
    if len(xs2) < 8:
        return [x for x in xs if x is not None]
    q1, _, q3 = statistics.quantiles(xs2, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [x for x in xs if x is not None and lo <= x <= hi]
