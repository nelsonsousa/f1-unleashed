"""
Lap Timing Processor — authoritative per-driver lap count + lap times.

Subscribes to: SessionStatus, TimingData, LapCount (race)
Emits:
  driverLaps:{num}  per-driver lap record (THIN — no accumulating history):
      { currentLap,
        lastLap:{lap,time,personalBest,overallBest,part}|null,
        bestLap:{lap,time,part}|null,
        overallBestLap:{lap,time}|null }
      currentLap is the lap the driver is on (NoL in P/Q, NoL+1 in race).
      Consumers needing per-lap history accumulate it from lastLap; seek/restore
      replays the full driverLaps history up to the offset.
      bestLap is the driver's fastest lap per F1's sticky PersonalFastest flag —
      this excludes out/in/cool laps (flagged PersonalFastest False), so a driver
      with only an out-lap done has bestLap=null (no valid reference).
      In QUALIFYING bestLap is scoped to the CURRENT part (Q1/Q2/Q3) and resets
      each part (card 63); overallBestLap is the session-wide best, kept as the
      delta-prediction reference. `part` (1/2/3, or null outside quali) tags the
      lap's qualifying part so the client can group laps by part (card 66).
      Outside qualifying bestLap == overallBestLap (no part resets).
  raceLaps          (race only) { currentLap, totalLaps }  — from LapCount
  fastestLap        { num, lap, time }  — emitted when a lap is OverallFastest
                    (the session-global fastest; client colours it purple)

Model (validated against 2026 data):
  - NumberOfLaps (NoL) is the authoritative per-driver lap counter and
    increments by +1 (one >+1 case across 30 sessions, a known source merge).
  - Lap *number* a NoL completes differs by session type:
        practice/qualifying : NoL=N means lap N is STARTING -> completes N-1
        race/sprint         : NoL=N means lap N has ENDED   -> completes N
    We index laps by the COMPLETED lap number (NoL-1 in P/Q, NoL in race).
  - LastLapTime pairing (handles out-of-order arrival):
      bundled (same msg as NoL)        -> the just-completed (highest) lap
      standalone, prev lap has no time -> assign to the just-completed lap
      standalone, prev lap has a time  -> for the in-progress lap; hold pending
        until the next NoL confirms its boundary.
  - Skipped NoL (e.g. 23->25): create empty slots for the skipped laps (they
    were driven but not recorded); time lands on the highest per the rules.
  - Lap counting only starts once the session has started (SessionStatus).
  - Red flags need no special handling — NoL/LastLapTime keep arriving normally.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_ms(s: str) -> Optional[int]:
    """Parse 'M:SS.mmm' (or 'MM:SS.mmm') into milliseconds."""
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


class LapTimingProcessor(Processor):
    """Authoritative lap count + lap times per driver."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"   # "race" covers race + sprint
        self._started = False
        self._nol: dict[str, int] = {}                       # num -> current NoL
        self._laps: dict[str, dict[int, dict]] = {}          # num -> {lap -> {time,pb,ob,part}}
        self._lap_part: dict[str, dict[int, Any]] = {}       # num -> {lap -> qualifying part it STARTED in}
        self._pending: dict[str, dict] = {}                  # num -> held lap-time
        self._best: dict[str, dict] = {}                     # num -> {lap,time,ms,part} — CURRENT part (display), reset per part
        self._session_best: dict[str, dict] = {}             # num -> {lap,time,ms} — session-wide, kept for delta prediction (card 63)
        self._part: Optional[int] = None                     # current qualifying part (1/2/3); None outside quali
        self._knocked: dict[str, bool] = {}                  # num -> KnockedOut (quali); their part-best is preserved across the next part reset
        # PersonalFastest/OverallFastest are STICKY F1 deltas — carry forward
        # until the field reappears (it flips False on the first non-improving lap).
        self._pb: dict[str, bool] = {}
        self._ob: dict[str, bool] = {}
        # Last recorded lap-time {time,pb,ob}, sticky — LastLapTime is omitted
        # when a lap equals the previous one exactly (F1 delta), so a completed
        # lap left timeless is recovered from this carried value.
        self._sticky_ll: dict[str, dict] = {}
        self._part_fastest_ms: Optional[int] = None          # current-part global fastest → purple; reset per part
        self._current_race_lap: Optional[int] = None
        self._total_race_laps: Optional[int] = None

    def subscribe(self) -> None:
        self._bus.on("SessionStatus", self._handle_session_status)
        self._bus.on("TimingData", self._handle_timing)
        self._bus.on("qualifyingPart", self._handle_qualifying_part)
        if self._is_race:
            self._bus.on("LapCount", self._handle_lap_count)

    # ── Qualifying part (Q1/Q2/Q3) ──
    def _handle_qualifying_part(self, data: Any, clock_time: datetime) -> None:
        """On a new qualifying part the DISPLAYED best resets to current-part
        only (card 63); the session-wide best is kept as the delta-prediction
        reference. Re-emit so each driver's shown best clears immediately,
        before they set a lap in the new part."""
        part = data if isinstance(data, int) else None
        if part is None or part == self._part:
            return
        self._part = part
        # Eliminated drivers keep the best lap from the part they were knocked
        # out in (it stays visible); everyone still in has their displayed best
        # cleared for the new part (card US3eJeKz).
        self._best = {n: b for n, b in self._best.items() if self._knocked.get(n)}
        self._part_fastest_ms = None
        for num in list(self._laps.keys()):
            self._emit(num, clock_time)

    # ── Session gate ──
    def _handle_session_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict) and data.get("Status") == "Started":
            self._started = True   # latches True on the first start (stays set across Q1/Q2/Q3, restarts)

    # ── Race lap counter ──
    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if data.get("TotalLaps"):
            self._total_race_laps = data["TotalLaps"]
        if "CurrentLap" in data:
            self._current_race_lap = data["CurrentLap"]
            self._bus.emit("raceLaps", {
                "currentLap": self._current_race_lap,
                "totalLaps": self._total_race_laps,
            }, clock_time)

    # ── Lap timing ──
    def _completed(self, nol: Optional[int]) -> int:
        """Number of laps completed when the counter reads `nol`."""
        if nol is None:
            return 0
        return nol if self._is_race else nol - 1

    def _handle_timing(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        # Capture the STICKY PersonalFastest/OverallFastest flags even BEFORE the
        # session starts: F1 sends each driver's initial flag state pre-green and
        # then only re-sends it on change, so gating this until _started drops the
        # first-lap PB flag — the lap then reads as un-flagged and its best is lost
        # (and with it overallBestLap, which the SLOW classifier needs).
        for num, d in lines.items():
            if isinstance(d, dict):
                self._capture_flags(num, d)
                if "KnockedOut" in d:
                    self._knocked[num] = bool(d["KnockedOut"])
        if not self._started:    # laps only count once the session has started
            return
        changed = set()
        for num, d in lines.items():
            if isinstance(d, dict) and self._process(num, d, clock_time):
                changed.add(num)
        for num in changed:
            self._emit(num, clock_time)

    def _capture_flags(self, num: str, d: dict) -> None:
        """Update the per-driver sticky PersonalFastest/OverallFastest state. The
        flags are per driver — one car's flag never carries to another."""
        llt = d.get("LastLapTime")
        if isinstance(llt, dict):
            if "PersonalFastest" in llt:
                self._pb[num] = bool(llt["PersonalFastest"])
            if "OverallFastest" in llt:
                self._ob[num] = bool(llt["OverallFastest"])

    def _process(self, num: str, d: dict, clock_time: datetime) -> bool:
        ll = None
        llt = d.get("LastLapTime")
        flag_only = False
        if isinstance(llt, dict):
            # Update the sticky flags whenever they appear (carry forward otherwise).
            had_flag = False
            if "PersonalFastest" in llt:
                self._pb[num] = bool(llt["PersonalFastest"])
                had_flag = True
            if "OverallFastest" in llt:
                self._ob[num] = bool(llt["OverallFastest"])
                had_flag = True
            if llt.get("Value"):
                ll = {"time": llt["Value"],
                      "personalBest": self._pb.get(num, False),
                      "overallBest": self._ob.get(num, False)}
            # F1 often sends the fastest flags a beat AFTER the lap time, in a
            # standalone message with NO "Value" key (card 84). A reset marker
            # has Value="" (key present) → not a late flag.
            elif had_flag and "Value" not in llt:
                flag_only = True
        changed = False
        advanced = False
        if "NumberOfLaps" in d:
            changed |= self._advance(num, int(d["NumberOfLaps"]), ll, clock_time)
            ll = None   # consumed as the bundled time
            advanced = True
        if ll is not None:
            changed |= self._standalone(num, ll, clock_time)
        elif flag_only:
            # Late PersonalFastest/OverallFastest flags (no Value) — apply to the
            # just-completed lap even when they arrive WITH the NumberOfLaps
            # advance. That advance sets the completed lap's time from the held
            # _pending value, which carried the STALE (pre-flag) personalBest, so
            # without re-applying the flags here the fastest lap is never counted
            # as best (its driver shows no best + purple lands on the next car).
            changed |= self._apply_late_flags(num, clock_time)
        return changed

    def _advance(self, num: str, new_nol: int, bundled_ll: Optional[dict],
                 clock_time: datetime) -> bool:
        prev = self._nol.get(num)
        self._nol[num] = new_nol
        laps = self._laps.setdefault(num, {})
        new_c = self._completed(new_nol)
        prev_c = self._completed(prev)
        # Tag the lap now STARTING (new_c+1) with the current part. A lap belongs
        # to the part it was DRIVEN in, not the part it completes in (card 82):
        # a part's in-lap completes only at the next part's start, so tagging at
        # completion would wrongly assign it to the new part. setdefault keeps
        # the part it first started in across a mid-lap part change.
        self._lap_part.setdefault(num, {}).setdefault(new_c + 1, self._part)
        # Same-time recovery: a previously-completed lap still timeless means
        # its LastLapTime was omitted because it equalled the prior lap (sticky
        # F1 delta) — no standalone ever arrived. Fill it from the carried value
        # BEFORE this message's bundled time updates the sticky value.
        if (prev_c >= 1 and laps.get(prev_c, {}).get("time") is None
                and num in self._sticky_ll):
            self._set_time(num, prev_c, self._sticky_ll[num], clock_time)
        # Create a slot for every newly-completed lap (skipped laps get an
        # empty slot — driven but not recorded by the source).
        for lap in range(max(prev_c, 0) + 1, new_c + 1):
            laps.setdefault(lap, {"time": None, "personalBest": False, "overallBest": False})
        # Assign the time of the just-completed (highest) lap.
        if new_c >= 1:
            if bundled_ll:
                self._set_time(num, new_c, bundled_ll, clock_time)
            elif num in self._pending:
                self._set_time(num, new_c, self._pending.pop(num), clock_time)
        return True

    def _standalone(self, num: str, ll: dict, clock_time: datetime) -> bool:
        c = self._completed(self._nol.get(num))
        laps = self._laps.get(num, {})
        if c >= 1 and laps.get(c, {}).get("time") is None:
            # P/Q: lap 1 is the OUT lap. A flying-lap time frequently arrives just
            # BEFORE NoL increments (so _completed lags a lap); filling the timeless
            # out lap with it mis-assigns the best to an OUT lap, which the client
            # then hides. Pend it so the imminent advance assigns it to the real
            # (flying) lap. (card P2A8g5O8)
            if not self._is_race and c == 1:
                self._pending[num] = ll
                return False
            self._set_time(num, c, ll, clock_time)  # the just-completed lap was still timeless
            return True
        # the prev lap already has a time -> this is the in-progress lap; hold it
        self._pending[num] = ll
        return False

    def _set_time(self, num: str, lap: int, ll: dict, clock_time: datetime) -> None:
        # Tag each lap with the part it was DRIVEN in (recorded at lap start;
        # None outside quali) so the client can group laps by part (card 66) and
        # the in-lap of a part isn't mis-assigned to the next (card 82).
        part = self._lap_part.get(num, {}).get(lap, self._part)
        self._laps[num][lap] = {**ll, "part": part}
        ms = _parse_ms(ll["time"])
        if ms is None:
            return
        self._sticky_ll[num] = dict(ll)   # carry forward for same-time recovery
        # PersonalFastest (sticky, per-driver) marks the driver's best of the
        # session — that alone drives the per-driver best (which also feeds the
        # per-part global fastest / purple via the per-part minimum). OverallFastest
        # is the session-global fastest flag, handled separately. Out/in/cool laps
        # are flagged PersonalFastest False so they never become the best.
        if ll.get("personalBest"):
            self._update_best(num, lap, ll["time"], ms, part, clock_time)

    def _update_best(self, num: str, lap: int, time: str, ms: int,
                     part: Optional[int], clock_time: datetime) -> None:
        """Record a flagged lap against the per-part best (display) and the
        session-wide best (delta reference). Shared by _set_time and the
        late-flag path (card 84)."""
        # Per-part best — drives the displayed best (standings + telemetry Best
        # view); reset each qualifying part (card 63). Only laps of the CURRENT
        # part count toward it.
        if part == self._part and (num not in self._best or ms < self._best[num]["ms"]):
            self._best[num] = {"lap": lap, "time": time, "ms": ms, "part": part}
            # Current-part global fastest → fastestLap (client colours it
            # purple). Computed from the per-driver bests, not the (per-driver,
            # sticky) OverallFastest flag which can read True for >1 car. Resets
            # each part with _best.
            if self._part_fastest_ms is None or ms < self._part_fastest_ms:
                self._part_fastest_ms = ms
                self._bus.emit("fastestLap", {"num": num, "lap": lap, "time": time}, clock_time)
        # Session-wide best — kept across parts as the delta-prediction
        # reference (card 63); emitted as overallBestLap, never reset.
        if num not in self._session_best or ms < self._session_best[num]["ms"]:
            self._session_best[num] = {"lap": lap, "time": time, "ms": ms}

    def _apply_late_flags(self, num: str, clock_time: datetime) -> bool:
        """A standalone PersonalFastest/OverallFastest=True update (no lap-time
        Value) — F1 sends the fastest flags a beat after the lap time. Re-apply
        them to the driver's last completed lap and re-evaluate best (card 84)."""
        if not (self._pb.get(num) or self._ob.get(num)):
            return False
        laps = self._laps.get(num, {})
        timed = [l for l, v in laps.items() if v.get("time")]
        if not timed:
            return False
        lap = max(timed)
        v = laps[lap]
        v["personalBest"] = self._pb.get(num, False)
        v["overallBest"] = self._ob.get(num, False)
        ms = _parse_ms(v["time"])
        if ms is None:
            return False
        self._update_best(num, lap, v["time"], ms, v.get("part"), clock_time)
        return True

    def _emit(self, num: str, clock_time: datetime) -> None:
        laps = self._laps.get(num, {})
        timed = [l for l, v in laps.items() if v.get("time")]
        # Quali: the "last lap" is the last lap of the CURRENT part, so at a new
        # part the cell blanks (server-driven) until a lap is set instead of
        # showing the previous part's lap. (Race: _part is None → all laps.)
        if self._part is not None:
            timed = [l for l in timed if self._lap_part.get(num, {}).get(l) == self._part]
        last = max(timed) if timed else None
        best = self._best.get(num)
        overall = self._session_best.get(num)
        # currentLap = the lap the driver is CURRENTLY on. P/Q: NoL (NoL=N means
        # lap N is starting). Race: NoL+1 (NoL=N means lap N has ended, so the
        # driver is on N+1). `_completed(nol)+1` gives both.
        nol = self._nol.get(num)
        current_lap = self._completed(nol) + 1 if nol is not None else None
        # Thin message: just the value(s) required. The per-lap time HISTORY is
        # not re-sent here — consumers accumulate it from `lastLap` as laps
        # arrive, and a seek/restore replays the full driverLaps history up to
        # the offset (see session._send_restore_extras), so accumulation is
        # seek-safe without re-sending the whole map on every emit.
        self._bus.emit(f"driverLaps:{num}", {
            "currentLap": current_lap,
            "lastLap": ({"lap": last, **laps[last]} if last is not None else None),
            # bestLap = CURRENT qualifying part (display); overallBestLap =
            # session-wide (delta-prediction reference). Equal outside quali.
            "bestLap": ({"lap": best["lap"], "time": best["time"], "part": best.get("part")} if best else None),
            "overallBestLap": ({"lap": overall["lap"], "time": overall["time"]} if overall else None),
        }, clock_time)
