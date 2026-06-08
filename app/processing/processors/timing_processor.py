"""
Timing Processor — per-driver timing data from TimingData/TimingAppData.

Subscribes to: TimingData, TimingAppData
Emits (all with subtopic = driver number):
  - driverGap:{num}     GapToLeader (race) or TimeDiffToFastest (practice/quali)
  - driverInt:{num}     IntervalToPositionAhead (race) or TimeDiffToPositionAhead
  - driverTiming:{num}  Current lap state: sectors, segments, lap time, fastest flags
  - driverTyres:{num}   Accumulated stint list, emitted on new compound

driverTiming resets on NumberOfLaps change (new lap). Before resetting,
the previous lap gets its lapTime set from LastLapTime and is emitted.

Also updates driverStatus for Retired/Stopped.
"""

import copy
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# Segment status code -> hex color
SEGMENT_COLORS = {
    0: None,           # no data
    2048: "#ffd700",   # yellow — completed, no improvement
    2049: "#00ff00",   # green — personal best
    2050: "#00ff00",   # green variant
    2051: "#ff00ff",   # purple — overall best
    2052: "#ff00ff",   # purple variant
    2064: "#ffffff",   # white — out lap / slow
}


def _segment_color(status: int) -> Optional[str]:
    return SEGMENT_COLORS.get(status)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _empty_lap() -> dict:
    """Create an empty lap timing state."""
    return {
        "lap": 0,
        "sectors": [
            {"value": None, "segments": [], "overallFastest": False, "personalFastest": False},
            {"value": None, "segments": [], "overallFastest": False, "personalFastest": False},
            {"value": None, "segments": [], "overallFastest": False, "personalFastest": False},
        ],
        "lapTime": None,
        "bestLapTime": None,
        "overallFastest": False,
        "personalFastest": False,
    }


class TimingProcessor(Processor):
    """Processes TimingData merge-patches into per-driver display messages."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        # Full merged state per driver
        self._state: dict[str, dict] = {}
        # Current lap timing state per driver
        self._lap_state: dict[str, dict] = {}
        # Track last emitted NumberOfLaps per driver
        self._laps: dict[str, int] = {}
        # Global current lap (race only — fallback for missing per-driver count)
        self._global_current_lap: int = 0
        # Per-driver position movement tracking — used to gate STOP.
        self._last_distpct: dict[str, float] = {}
        self._last_movement_ts: dict[str, datetime] = {}
        # Tyre stint tracking per driver
        self._stint_state: dict[str, dict] = {}  # num -> {raw, emitted_stints}
        # Personal best laps per driver
        self._best_laps: dict[str, dict] = {}  # num -> {lap, time, compound, new, tyreLaps}
        # Cumulative lap times per driver. Keys are sequential 1..N — no
        # holes (per SME rule 2). Lap 1 is the placeholder (no duration:
        # session start / OUT lap from pit) and always stores None. Each
        # non-empty LastLapTime value seen advances a counter and gets
        # assigned to lap (counter + 1).
        self._lap_times: dict[str, dict[int, Optional[str]]] = {}
        # Count of non-empty LastLapTime values seen per driver. The next
        # non-empty LL goes to lap (count+1) since lap 1 is the
        # placeholder. LL=None / LL="" / no LL in patch do NOT advance.
        self._ll_count: dict[str, int] = {}
        # Sticky LastLap state — F1's TimingData patches are differential;
        # when an NL bump arrives with no LastLapTime field, the value is
        # unchanged from prior (= F1 omits duplicate values). We apply the
        # sticky value at the NL bump unless we're under a red flag (in
        # which case the in-progress lap is discarded per SME rule).
        self._sticky_ll: dict[str, Optional[str]] = {}
        # Previous NumberOfLaps per driver — used to detect NL skips
        # (e.g. F1 jumps from NL=23 to NL=25; we fill the missing label
        # with a null lap-time entry so the lap list stays contiguous).
        self._prev_nl: dict[str, int] = {}
        # True if a STANDALONE LastLapTime (= no NumberOfLaps in same
        # patch) has fired since the most recent NL bump. F1 often
        # sends the LL value standalone just before the NL bump that
        # confirms the lap end — both refer to the SAME lap, so the
        # sticky-LL fallback on the following NL bump must NOT
        # double-assign.
        self._standalone_since_nl: dict[str, bool] = {}
        # Current track status. While trackStatus=="RED", sticky LL must
        # not be applied to NL bumps (the lap is in-progress under red
        # flag and should be discarded entirely; the LL value for that
        # lap arrives later, with a subsequent NL bump after resumption).
        self._under_red_flag: bool = False
        # Segment layout (emitted once)
        self._seg_layout_emitted = False

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)
        self._bus.on("TimingAppData", self._handle_app_data)
        # F1 stopped sending NumberOfLaps per-driver in race TimingData for
        # 2026 — we now derive each driver's current lap from the global
        # LapCount topic combined with per-driver LastLapTime arrivals.
        self._bus.on("LapCount", self._handle_lap_count)
        # We also track whether each driver is still moving so we can
        # suppress F1's spurious "Stopped: true" flags during telemetry
        # glitches (speed sensor reads zero while the car is actually
        # rolling and position keeps changing).
        self._bus.on("position", self._handle_position)
        # Track-status subscription drives the red-flag gate for
        # sticky-LL application.
        self._bus.on("trackStatus", self._handle_track_status)

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, str):
            self._under_red_flag = (data == "RED")

    # Suppress STOP if the driver moved in the last ~3 s.
    _STOP_STATIONARY_WINDOW_S = 3.0
    # Min change in distPct (0..100) to count as movement.
    _STOP_MOVEMENT_EPS = 0.05

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, coords in data.items():
            if not isinstance(coords, list) or len(coords) < 3:
                continue
            dist = coords[2]
            prev = self._last_distpct.get(num)
            if prev is None or abs(dist - prev) >= self._STOP_MOVEMENT_EPS:
                self._last_movement_ts[num] = clock_time
            self._last_distpct[num] = dist

    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        if not self._is_race or not isinstance(data, dict):
            return
        cur = data.get("CurrentLap")
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            return
        if cur < 1:
            return
        self._global_current_lap = cur
        # Bootstrap any driver whose lap state hasn't been initialized yet.
        # We don't overwrite existing per-driver counters because some
        # drivers may already be on a different lap (lapped, retired, etc.).
        for num in list(self._state.keys()):
            lap_state = self._lap_state.get(num)
            if lap_state is None:
                self._lap_state[num] = _empty_lap()
                self._lap_state[num]["lap"] = cur
                self._laps[num] = cur

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        # Detect segment layout from first TimingData with Segments arrays
        if not self._seg_layout_emitted:
            for num2, drv in lines.items():
                if not isinstance(drv, dict):
                    continue
                sectors = drv.get("Sectors")
                if not sectors:
                    continue
                items = sectors if isinstance(sectors, list) else [sectors[k] for k in sorted(sectors.keys(), key=int)]
                layout = []
                for s in items:
                    if not isinstance(s, dict):
                        continue
                    segs = s.get("Segments")
                    if isinstance(segs, list):
                        layout.append(len(segs))
                    elif isinstance(segs, dict):
                        layout.append(len(segs))
                    else:
                        layout.append(0)
                if len(layout) == 3 and all(c > 0 for c in layout):
                    self._bus.emit("segmentLayout", layout, clock_time)
                    self._seg_layout_emitted = True
                    break

        for num, patch in lines.items():
            if not isinstance(patch, dict):
                continue

            # Merge into full state
            if num not in self._state:
                self._state[num] = {}
            _deep_merge(self._state[num], patch)

            # --- driverStatus: Retired / Stopped ---
            if patch.get("Retired") is True:
                self._bus.emit(f"driverStatus:{num}", "RET", clock_time)
            elif patch.get("Stopped") is True:
                # F1 sometimes flags Stopped=true during a brief speed-sensor
                # dropout while the car keeps rolling. Only emit STOP if the
                # driver has actually been stationary for the last few seconds.
                last_move = self._last_movement_ts.get(num)
                if last_move is None:
                    # We've never seen them move yet — accept the flag.
                    self._bus.emit(f"driverStatus:{num}", "STOP", clock_time)
                else:
                    elapsed = (clock_time - last_move).total_seconds()
                    if elapsed >= self._STOP_STATIONARY_WINDOW_S:
                        self._bus.emit(f"driverStatus:{num}", "STOP", clock_time)
                    # else: car is still rolling, ignore the flag.

            # --- driverGap / driverInt (race only) ---
            if self._is_race:
                if "GapToLeader" in patch:
                    self._bus.emit(f"driverGap:{num}", patch["GapToLeader"], clock_time)
                if "IntervalToPositionAhead" in patch:
                    val = patch["IntervalToPositionAhead"]
                    if isinstance(val, dict):
                        val = val.get("Value", "")
                    self._bus.emit(f"driverInt:{num}", val, clock_time)

            # --- driverTiming: apply sectors/segments/times to current lap FIRST ---
            if num not in self._lap_state:
                self._lap_state[num] = _empty_lap()
                # Race: bootstrap lap from global LapCount so the timing
                # gate (lap > 0) opens even though per-driver NumberOfLaps
                # never arrives in 2026 race data.
                if self._is_race and self._global_current_lap > 0:
                    self._lap_state[num]["lap"] = self._global_current_lap
                    self._laps[num] = self._global_current_lap
            lap = self._lap_state[num]
            timing_changed = False

            # Sectors (apply before NumberOfLaps so last sector lands on correct lap)
            sectors_patch = patch.get("Sectors")
            if sectors_patch:
                items = sectors_patch.items() if isinstance(sectors_patch, dict) else enumerate(sectors_patch)
                for idx, sec in items:
                    if not isinstance(sec, dict):
                        continue
                    i = int(idx)
                    if i >= len(lap["sectors"]):
                        continue
                    s = lap["sectors"][i]

                    if "Value" in sec and sec["Value"] != "":
                        s["value"] = sec["Value"]
                        timing_changed = True
                    if "OverallFastest" in sec:
                        s["overallFastest"] = sec["OverallFastest"]
                        timing_changed = True
                    if "PersonalFastest" in sec:
                        s["personalFastest"] = sec["PersonalFastest"]
                        timing_changed = True

                    seg_patch = sec.get("Segments")
                    if seg_patch:
                        seg_items = seg_patch.items() if isinstance(seg_patch, dict) else enumerate(seg_patch)
                        for seg_idx, seg in seg_items:
                            if not isinstance(seg, dict):
                                continue
                            si = int(seg_idx)
                            while len(s["segments"]) <= si:
                                s["segments"].append(None)
                            if "Status" in seg:
                                s["segments"][si] = _segment_color(seg["Status"])
                                timing_changed = True

            # BestLapTime
            blt = patch.get("BestLapTime")
            if isinstance(blt, dict):
                if "Value" in blt and blt["Value"] != "":
                    lap["bestLapTime"] = blt["Value"]
                    timing_changed = True

                    # Track personal best with tyre info
                    best_lap_num = blt.get("Lap")
                    if best_lap_num is not None:
                        tyre_info = self._get_tyre_at_lap(num, int(best_lap_num))
                        self._best_laps[num] = {
                            "lap": int(best_lap_num),
                            "time": blt["Value"],
                            **tyre_info,
                        }
                        self._emit_fastest_laps(clock_time)

            # LastLapTime — update sticky + display + fastest flags.
            # Assignment to lap_times happens later: STANDALONE LL (=
            # patch has LastLapTime but no NumberOfLaps) assigns
            # immediately below; LL bundled with an NL bump defers to
            # the NL block so that NL-skip null fills run BEFORE the LL
            # value gets assigned.
            llt = patch.get("LastLapTime")
            new_ll_val: Optional[str] = None
            if isinstance(llt, dict):
                val = llt.get("Value")
                if val:
                    new_ll_val = val
                    self._sticky_ll[num] = val
                    lap["lapTime"] = val
                    timing_changed = True
                if "OverallFastest" in llt:
                    lap["overallFastest"] = llt["OverallFastest"]
                    timing_changed = True
                if "PersonalFastest" in llt:
                    lap["personalFastest"] = llt["PersonalFastest"]
                    timing_changed = True

            # Standalone LL (= no NumberOfLaps in this patch) assigns
            # immediately as lap (current_NL)'s time. Mark the standalone
            # flag so the NEXT NL bump doesn't double-assign via the
            # sticky-LL fallback.
            if new_ll_val is not None and "NumberOfLaps" not in patch:
                current_nl = self._laps.get(num, 0)
                if current_nl >= 1:
                    self._assign_lap_time(num, new_ll_val, clock_time,
                                          lap_number=current_nl)
                else:
                    self._assign_lap_time(num, new_ll_val, clock_time)
                self._standalone_since_nl[num] = True

            # Emit current lap if changed (before potential new-lap reset)
            if timing_changed and lap["lap"] > 0:
                self._bus.emit(f"driverTiming:{num}", copy.deepcopy(lap), clock_time)

            # --- New lap: start fresh ---
            new_lap_num: Optional[int] = None
            if "NumberOfLaps" in patch:
                # Pre-2026 / qualifying / practice path
                new_lap_num = int(patch["NumberOfLaps"])
            elif self._is_race and isinstance(llt, dict) and llt.get("Value"):
                # Race in 2026: F1 doesn't send NumberOfLaps anymore. The
                # arrival of a LastLapTime means this driver just completed
                # the lap that lap_state was holding — start the next one.
                new_lap_num = (self._laps.get(num) or lap.get("lap") or 0) + 1

            if new_lap_num is not None and new_lap_num != self._laps.get(num):
                # NL-skip detection (per SME): if F1 jumps NL by more
                # than 1 (e.g. NL=23 → NL=25 for LAW Suzuka FP2 where F1
                # merged two real laps into one then skipped a label),
                # insert null lap-time entries for each missing label so
                # the lap list stays contiguous from FIA's perspective.
                prev_nl = self._prev_nl.get(num, 0)
                if prev_nl >= 1 and new_lap_num > prev_nl + 1:
                    # Fill skipped lap slots [prev_nl, new_lap_num - 2]
                    # with None. Lap (new_lap_num - 1) gets its value
                    # below from bundled LL or sticky.
                    for missing_ln in range(prev_nl, new_lap_num - 1):
                        self._assign_lap_time(num, None, clock_time,
                                              lap_number=missing_ln)
                # Apply LL value for this NL bump:
                #   F1's TimingData patches are differential; an OMITTED
                #   LastLapTime field means "value unchanged from prior".
                #   In MOST cases that means we should apply the sticky
                #   value (= F1 omitted because the new lap had the
                #   same time as the prior lap, e.g. SAI Shanghai
                #   NL=49 = identical to lap 48).
                #
                #   BUT NL bumps that carry pit-state transitions
                #   (PitOut=True, InPit=True in the same patch) are
                #   "starting a new lap from pit" — not lap completions.
                #   The previous lap's time was reported via the
                #   standalone LL just before. Applying sticky here
                #   would duplicate that value into a phantom lap.
                #
                #   Pure-NL bumps (= patch contains ONLY NumberOfLaps,
                #   no Sectors/Speeds/LL/pit) are F1 artifacts (e.g.
                #   LAW Suzuka FP3 NL=17, no other data). Treat as
                #   phantom — no assignment.
                #
                #   Under red flag: lap is discarded entirely.
                #
                # Cases:
                #   1) LL bundled in patch → assign now.
                #   2) Standalone LL already fired since last NL bump →
                #      don't double-assign.
                #   3) Patch has InPit/PitOut → no sticky (= pit-state
                #      transition, not a lap completion).
                #   4) Patch has only NumberOfLaps (= no Sectors,
                #      Speeds, LL, InPit, PitOut) → no sticky.
                #   5) Under red flag → no sticky.
                #   6) Otherwise → apply sticky.
                pit_signal = "InPit" in patch or "PitOut" in patch
                pure_nl_only = set(patch.keys()) == {"NumberOfLaps"}
                # Per F1 NL convention: bundled LL with NL=K bump =
                # time of lap (K-1). Skip if K=1 (no previous lap).
                bundled_lap = new_lap_num - 1
                if bundled_lap >= 1:
                    if new_ll_val is not None:
                        self._assign_lap_time(num, new_ll_val, clock_time,
                                              lap_number=bundled_lap)
                    elif self._standalone_since_nl.get(num):
                        pass
                    elif pit_signal or pure_nl_only or self._under_red_flag:
                        pass
                    else:
                        sticky = self._sticky_ll.get(num)
                        if sticky is not None:
                            self._assign_lap_time(num, sticky, clock_time,
                                                  lap_number=bundled_lap)
                self._standalone_since_nl[num] = False
                self._prev_nl[num] = new_lap_num

                # Snapshot the just-completed lap as its own topic so the
                # client always has the previous lap available — even
                # after a seek where get_state_at returns only the
                # post-rollover empty driverTiming.
                if lap["lap"] > 0:
                    self._bus.emit(
                        f"driverLastLap:{num}",
                        copy.deepcopy(lap),
                        clock_time,
                    )
                self._laps[num] = new_lap_num
                self._lap_state[num] = _empty_lap()
                self._lap_state[num]["lap"] = new_lap_num
                # Placeholder removed (2026-06-03 v5): lap_times[K] now
                # holds lap K's actual time directly. No phantom lap 1
                # None entry created here.
                # Emit a fresh (empty-sector) driverTiming for the new
                # lap immediately so the client clears any leftover
                # prev-lap sector cells before the next sector-progress
                # update arrives. Without this the prev lap's S3 cell
                # stays visible because the client still has the prior
                # emit cached as `state.timing[num]`.
                self._bus.emit(
                    f"driverTiming:{num}",
                    copy.deepcopy(self._lap_state[num]),
                    clock_time,
                )

    def _assign_lap_time(self, num: str, val: str, clock_time: datetime,
                         lap_number: Optional[int] = None) -> None:
        """Assign a LastLapTime value to lap_times[lap_number] where
        lap_number is the actual lap the LL refers to (= NOT the count
        of LL assignments).

        Per F1 NL convention:
          - NL=K marks the START of lap K (= lap K-1 just ended).
          - LL bundled with NL=K bump = time of lap K-1.
              → caller passes lap_number = new_NL - 1.
          - LL standalone (no NL change) while driver at dp 90-100%
            just before NL=K+1 → time of lap K (= the lap currently
            being driven, just ended at pit-lane S/F).
              → caller passes lap_number = current_NL.

        Fills earlier slots with None for contiguity (e.g. if NL=2 had
        LL=null then lap_times[1] won't yet exist; if standalone LL
        arrives for lap 5 first, lap_times[1..4] all = None).

        If lap_number is None (legacy fallback): use ll_count-based
        indexing without the +1 shift."""
        if lap_number is None:
            self._ll_count[num] = self._ll_count.get(num, 0) + 1
            lap_number = self._ll_count[num]
        else:
            self._ll_count[num] = max(self._ll_count.get(num, 0), lap_number)
        if lap_number < 1:
            return
        if num not in self._lap_times:
            self._lap_times[num] = {}
        for ln in range(1, lap_number):
            if ln not in self._lap_times[num]:
                self._lap_times[num][ln] = None
        self._lap_times[num][lap_number] = val
        self._bus.emit(
            f"driverLapTimes:{num}", dict(self._lap_times[num]), clock_time
        )

    def finalize_session(self, last_clock: datetime) -> None:
        """At session end: for each driver, ensure lap_times has entries
        from 1 to NL_max (= classification count). The "in-progress" lap
        (= the last NL value reached, whose time was never published
        because NL never bumped past it) gets a None entry — matches
        FIA's classification count and tags the lap as "started but no
        time"."""
        for num, max_nl in self._laps.items():
            if max_nl is None or max_nl < 1:
                continue
            if num not in self._lap_times:
                self._lap_times[num] = {}
            changed = False
            for ln in range(1, max_nl + 1):
                if ln not in self._lap_times[num]:
                    self._lap_times[num][ln] = None
                    changed = True
            if changed:
                self._bus.emit(
                    f"driverLapTimes:{num}",
                    dict(self._lap_times[num]),
                    last_clock,
                )

    # ── Tyre Stints ──

    def _get_tyre_at_lap(self, num: str, lap_num: int) -> dict:
        """Get tyre compound, new/used, and laps on tyre at the start of a given lap."""
        ss = self._stint_state.get(num)
        if not ss:
            return {"compound": None, "new": False, "tyreLaps": 0}

        # Build stint list with session lap ranges
        stints = []
        session_lap = 1
        for idx_str in sorted(ss["raw"].keys(), key=int):
            raw = ss["raw"][idx_str]
            compound = raw.get("Compound")
            if not compound or compound == "UNKNOWN":
                continue
            is_new = raw.get("New") in ("true", True)
            start_laps = int(raw.get("StartLaps", 0))
            total_laps = int(raw.get("TotalLaps", 0))
            tyre_laps = total_laps - start_laps
            stint_start = session_lap
            stint_end = session_lap + max(tyre_laps, 1) - 1
            stints.append({
                "compound": compound,
                "new": is_new,
                "startLaps": start_laps,
                "stintStart": stint_start,
                "stintEnd": stint_end,
            })
            session_lap = stint_end + 1

        # Find which stint contains lap_num
        for stint in reversed(stints):
            if lap_num >= stint["stintStart"]:
                laps_into_stint = lap_num - stint["stintStart"]
                return {
                    "compound": stint["compound"],
                    "new": stint["new"],
                    "tyreLaps": stint["startLaps"] + laps_into_stint,
                }

        return {"compound": None, "new": False, "tyreLaps": 0}

    def _emit_fastest_laps(self, clock_time: datetime) -> None:
        """Emit sorted list of all drivers' personal best laps."""
        entries = []
        for num, best in self._best_laps.items():
            entries.append({
                "driver": num,
                "lap": best["lap"],
                "time": best["time"],
                "compound": best.get("compound"),
                "new": best.get("new", False),
                "tyreLaps": best.get("tyreLaps", 0),
            })
        entries.sort(key=lambda e: e["time"])
        self._bus.emit("fastestLaps", entries, clock_time)

    def _handle_app_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        for num, patch in lines.items():
            if not isinstance(patch, dict):
                continue
            stints_patch = patch.get("Stints")
            if stints_patch is None:
                continue

            if num not in self._stint_state:
                self._stint_state[num] = {"raw": {}, "emitted_stints": set()}
            ss = self._stint_state[num]

            # Merge stint patches — handle both array and dict formats
            items = stints_patch.items() if isinstance(stints_patch, dict) else enumerate(stints_patch)
            for idx, stint_patch in items:
                if not isinstance(stint_patch, dict):
                    continue
                idx_str = str(idx)
                if idx_str not in ss["raw"]:
                    ss["raw"][idx_str] = {}
                prev_total = ss["raw"][idx_str].get("TotalLaps")
                _deep_merge(ss["raw"][idx_str], stint_patch)
                cur_compound = ss["raw"][idx_str].get("Compound")
                new_total = ss["raw"][idx_str].get("TotalLaps")

                is_first_compound = (
                    cur_compound and cur_compound != "UNKNOWN"
                    and idx_str not in ss["emitted_stints"]
                )
                if is_first_compound:
                    ss["emitted_stints"].add(idx_str)

                # Emit on: (a) stint first gets a real compound, or
                # (b) TotalLaps changed for an already-emitted stint
                # (drives the per-tyre lap counter shown in the UI).
                if is_first_compound or (
                    idx_str in ss["emitted_stints"] and new_total != prev_total
                ):
                    self._emit_tyres(num, ss, clock_time)

    def _emit_tyres(self, num: str, ss: dict, clock_time: datetime) -> None:
        """Build and emit accumulated stint list."""
        stints_out = []
        session_lap = 1

        valid_indices = [i for i in sorted(ss["raw"].keys(), key=int)
                        if ss["raw"][i].get("Compound") and ss["raw"][i]["Compound"] != "UNKNOWN"]

        for i, idx_str in enumerate(valid_indices):
            raw = ss["raw"][idx_str]
            is_new = raw.get("New") in ("true", True)
            start_laps = int(raw.get("StartLaps", 0))
            total_laps = int(raw.get("TotalLaps", 0))
            is_last = (i == len(valid_indices) - 1)

            stints_out.append({
                "lap": session_lap,
                "compound": raw["Compound"],
                "new": is_new,
                "startLaps": start_laps,
                "totalLaps": total_laps,
                "current": is_last,
            })

            tyre_laps = total_laps - start_laps
            if tyre_laps > 0:
                session_lap += tyre_laps
            else:
                session_lap += 1

        self._bus.emit(f"driverTyres:{num}", stints_out, clock_time)
