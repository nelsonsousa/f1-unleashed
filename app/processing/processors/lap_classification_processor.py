"""
Lap Classification Processor — real-time lap pace analysis.

Subscribes to: driverStatus:{num}, TimingData, CarData.z, position
Emits:
  ~lap-status           Internal: {driverNum, lapNumber, status} for on-track/run-plan
  lapClassification:{n} Per-driver (DB): {lap, status} for seek restore

Classification rules:
  OUT   — first lap after pit exit
  PIT   — any lap ending in pits (overrides all)
  COOL  — slow detected (within first 80% of lap distance)
  PUSH  — fast lap (first fast after slow/out, or all fast in qualifying)
  LONG  — consecutive fast laps in practice
  RACE  — all timed laps in race sessions

Slow detection: speed >20 km/h below driver's best lap reference for 4+
consecutive samples, checked up to 80% distance.  Once slow, stays slow
(unless lap ends in pit).
"""

import logging
from bisect import bisect_left
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)

SLOW_THRESHOLD_KMH = 20.0
SLOW_WINDOW_S = 5.0
SLOW_OUTLIER_RATIO = 0.10
SLOW_CHECK_MAX_PCT = 33.0

# Spell-aware retroactive reclassification (SME 2026-06-03):
#   If a previous fast lap in the same dry spell is at least this many
#   ms slower than the current lap, the previous lap is reclassified
#   as COOL. In a dry spell following a wet spell only the immediate
#   predecessor is checked.
RETRO_RECLASS_THRESHOLD_MS = 10_000

DRY_COMPOUNDS = frozenset({"SOFT", "MEDIUM", "HARD", "C1", "C2", "C3", "C4", "C5"})
WET_COMPOUNDS = frozenset({"INTERMEDIATE", "INTER", "WET"})


def _compound_type(compound: str) -> str:
    """Return 'wet' for INTER/WET, 'dry' otherwise (default for unknown/test)."""
    if not isinstance(compound, str):
        return "dry"
    c = compound.upper().strip()
    if c in WET_COMPOUNDS:
        return "wet"
    return "dry"


def _parse_time_ms(s: Any) -> Optional[int]:
    """Parse F1 lap-time string 'M:SS.mmm' into milliseconds."""
    if not isinstance(s, str):
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        mins = int(parts[0])
        sp = parts[1].split(".")
        secs = int(sp[0])
        ms = int(sp[1].ljust(3, "0")[:3]) if len(sp) > 1 else 0
        return mins * 60000 + secs * 1000 + ms
    except (ValueError, IndexError):
        return None


class LapClassificationProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_practice = session_type == "practice"
        self._is_qualifying = session_type == "qualifying"
        self._is_race = session_type == "race"
        # Race mode: detect lights-out via SessionStatus → "Started" and
        # WIPE all pre-race classifications. Pre-race installation laps,
        # garage→grid pit transitions etc. consume F1's NumberOfLaps
        # counter and confuse the classifier — at race start we throw
        # all of that out and treat the current F1 lap as race lap N
        # (= lap 1 in the user's mental model when F1 lap == 1).
        self._race_started: bool = False

        self._pending_out: dict[str, bool] = {}
        self._collecting: dict[str, bool] = {}
        self._timing_lap: dict[str, int] = {}

        self._lap_type: dict[str, str] = {}           # "OUT" | "TIMED"
        self._classification: dict[str, str] = {}
        self._slow_window: dict[str, list[tuple[float, bool]]] = {}  # (epoch_s, is_slow)
        self._is_slow: dict[str, bool] = {}
        self._prev_classification: dict[str, str] = {}

        self._prev_fast_avg_speed: dict[str, float] = {}  # avg speed of prev fast lap
        self._last_dist_pct: dict[str, float] = {}
        self._best_ref: dict[str, list[tuple[float, float]]] = {}
        self._best_ref_pcts: dict[str, list[float]] = {}
        self._best_avg_speed: dict[str, float] = {}
        self._lap_speeds: dict[str, list[tuple[float, float]]] = {}

        self._lap_history: dict[str, dict[int, str]] = {}  # num -> {lap -> status}
        self._last_emitted_cls: dict[str, str] = {}
        # Per-(driver, lap) emit dedup. _last_emitted_cls above is a
        # per-driver gate that bounces when emits for different laps
        # interleave (= lap 2 emit sets it to PUSH, then lap 3 emit to
        # LONG, then lap 2 wants to emit PUSH again — gate says OK,
        # re-emit). This kills that re-emit at source.
        self._last_emitted_per_lap: dict[tuple, str] = {}
        # Pending header for the single emit produced at the end of each
        # top-level handler. _emit_status mutates state synchronously
        # and parks (lap, status) here; _flush_dirty() drains it into a
        # single bus message with the full lap-history snapshot.
        self._dirty_header: dict[str, tuple] = {}

        # F1's reported lap times (ms). Populated from driverLapTimes
        # events. Used by the qualifying first-PUSH-after-OUT fallback
        # rule (line ~245): if the just-completed flying lap is more
        # than 2 s slower than the driver's current session best, treat
        # it as COOL instead of PUSH.
        self._lap_actual_ms: dict[str, dict[int, int]] = {}

        # ── Spell tracking (SME 2026-06-03) ──
        # A "spell" is a continuous period on the same tyre type
        # (dry: SOFT/MEDIUM/HARD; wet: INTER/WET). Same-type tyre
        # changes (soft→medium pit stop) stay in the same spell.
        # Spells reset the speed reference and fast-laps list. Wet
        # spells skip fast/slow classification entirely (= every lap
        # is WET unless OUT/IN/STOP).
        self._compound: dict[str, str] = {}
        self._spell_type: dict[str, str] = {}  # "dry" | "wet"
        self._spell_idx: dict[str, int] = {}
        # True if the current dry spell was preceded by a wet spell;
        # in that case retroactive reclass only checks the immediate
        # predecessor (= track is drying, evolution distorts lap times).
        self._spell_followed_wet: dict[str, bool] = {}
        # Per-spell best lap time + per-position speed reference (from
        # the lap that set the best). Updated each time the spell-best
        # improves. Used by the within-lap COOL detector.
        self._spell_best_ms: dict[str, Optional[int]] = {}
        self._spell_best_ref: dict[str, list[tuple[float, float]]] = {}
        self._spell_best_pcts: dict[str, list[float]] = {}
        # Per-spell list of (lap, lap_time_ms) for laps currently
        # classified PUSH/LONG. Used as the scan-set for retroactive
        # reclass when a new fast lap arrives. Demoted laps are removed.
        self._spell_fast_laps: dict[str, list[tuple[int, int]]] = {}
        # Per-driver per-lap CarData speed samples (= (dist_pct, speed))
        # captured during the lap. When the lap is finalized as the new
        # spell best, this list becomes the new spell_best_ref.
        self._current_lap_speeds: dict[str, list[tuple[float, float]]] = {}
        # Locked-classification set: STOP / IN are irrevocable except
        # by each other (STOP overrides IN; STOP can be revoked only
        # by un-retire). OUT and COOL are also locked, but can be
        # overridden by IN or STOP.
        self._stopped: dict[str, bool] = {}

        # Per-lap qualifying segment (1=Q1, 2=Q2, 3=Q3) — used by the
        # telemetry lap-list to group pills into Q1/Q2/Q3 sections with
        # numbering reset per segment. None outside qualifying.
        self._lap_segment: dict[str, dict[int, int]] = {}
        self._current_qual_part: int = 0

    def subscribe(self) -> None:
        # Wrap every subscription so that one batched lapClassification
        # message fires AFTER the handler returns (= one emit per inbound
        # event, with the full lap-history snapshot built from the
        # already-mutated state). See _flush_dirty.
        self._bus.on("CarData.z", self._flush_after(self._handle_car_data))
        self._bus.on("TimingData", self._flush_after(self._handle_timing))
        self._bus.on("qualifyingPart", self._flush_after(self._handle_qualifying_part))
        self._bus.on("*", self._flush_after(self._handle_wildcard))
        if self._is_race:
            self._bus.on("sessionInfo", self._flush_after(self._handle_session_data))

    def _flush_after(self, fn):
        """Wrap a bus handler so that ``_flush_dirty`` runs after the
        handler returns. Works for both the (data, clock_time) and
        (topic, data, clock_time) bus signatures — clock_time is
        always the trailing positional arg."""
        def wrapped(*args):
            try:
                fn(*args)
            finally:
                ct = args[-1] if args else None
                if isinstance(ct, datetime):
                    self._flush_dirty(ct)
        return wrapped

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        """Race-only: detect sessionStatus → "Started" (= lights-out)
        and WIPE every driver's pre-race classification state.

        The signal comes through the processed `sessionInfo` topic
        (= our pre-processor produces a stable payload with field
        `sessionStatus`). Pre-race installation laps + garage→grid
        pit-status transitions get classified as OUT/PIT against F1's
        NumberOfLaps counter even though they're not racing laps. At
        lights-out we throw all of that out and re-emit each driver's
        current F1 lap as RACE."""
        if not self._is_race or self._race_started:
            return
        if not isinstance(data, dict):
            return
        if data.get("sessionStatus") == "Started":
            self._on_race_start(clock_time)

    def _on_race_start(self, clock_time: datetime) -> None:
        self._race_started = True
        # Snapshot driver set from everywhere we've tracked state.
        drivers = (set(self._timing_lap.keys())
                   | set(self._lap_history.keys())
                   | set(self._classification.keys())
                   | set(self._collecting.keys()))
        for num in drivers:
            # Read the driver's pre-race "on track or in pit" state.
            # Grid starters: _collecting is True (= on grid = on track).
            # Pit-lane starters: _collecting is False (= in pit lane,
            # will leave AFTER lights-out). The pit-lane case is rare
            # but real — Montréal 2026 had LIN + STR start from pit;
            # Miami 2026 had HAD. We MUST keep them as PIT, not flip
            # them to RACE.
            was_collecting = self._collecting.get(num, True)

            # Wipe pre-race per-lap history + emit dedup.
            self._lap_history[num] = {}
            self._last_emitted_per_lap = {
                k: v for k, v in self._last_emitted_per_lap.items()
                if k[0] != num
            }
            self._last_emitted_cls[num] = ""
            self._pending_out[num] = False

            cur_lap = self._timing_lap.get(num) or 1
            if was_collecting:
                # Grid starter: clean RACE start.
                self._classification[num] = "RACE"
                self._lap_type[num] = "TIMED"
                self._prev_classification[num] = "RACE"
                self._collecting[num] = True
                if cur_lap >= 1:
                    self._set_class(num, cur_lap, "RACE", clock_time)
            else:
                # Pit-lane starter: stays PIT until F1 emits the OUT
                # transition when they leave pit lane. Normal flow
                # then takes over (PIT → OUT → RACE).
                self._classification[num] = "PIT"
                self._lap_type[num] = "IN"
                self._prev_classification[num] = "PIT"
                self._collecting[num] = False
                if cur_lap >= 1:
                    self._set_class(num, cur_lap, "PIT", clock_time)

    def _handle_qualifying_part(self, data: Any, clock_time: datetime) -> None:
        # Only 1, 2, 3 are real qualifying segments. QualifyingPart=0
        # (or anything outside 1-3) means qualifying hasn't started yet
        # and must NOT touch _current_qual_part — otherwise late
        # post-session re-emits (F1 has been seen to flip back to 0
        # after Q3 ends) corrupt the segment tagging of any newly
        # arriving laps.
        if not isinstance(data, int) or data < 1 or data > 3:
            return
        prev = self._current_qual_part
        self._current_qual_part = data
        # First qualifyingPart=1 arrives early but may follow a few laps
        # that started while `_current_qual_part` was still 0 — backfill
        # those so they end up grouped under Q1, not Q0.
        if prev == 0 and data == 1:
            for lap_map in self._lap_segment.values():
                for lap in list(lap_map):
                    if lap_map[lap] == 0:
                        lap_map[lap] = 1
        # Race in 2026 doesn't carry per-driver NumberOfLaps; we use the
        # global LapCount + per-driver LastLapTime arrival to detect lap
        # boundaries. Both reach us via _handle_timing (LastLapTime) and
        # this dedicated handler (LapCount).
        if self._is_race:
            self._bus.on("LapCount", self._flush_after(self._handle_lap_count))

    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        try:
            cur = int(data.get("CurrentLap"))
        except (TypeError, ValueError):
            return
        # Bootstrap drivers we've already seen but whose lap is unset.
        for num in list(self._collecting.keys()):
            if num not in self._timing_lap:
                self._timing_lap[num] = cur

    # ── Spell tracking (SME 2026-06-03) ──

    def _on_tyres_change(self, num: str, data: Any, clock_time: datetime) -> None:
        """driverTyres payload = list of stint dicts. The last with
        current=True is the currently-running stint; its `compound`
        field gives the current tyre. We detect spell transitions
        (dry↔wet) and reset the speed reference + fast-laps list."""
        if not isinstance(data, list): return
        current = None
        for stint in data:
            if isinstance(stint, dict) and stint.get("current"):
                current = stint
        if current is None:
            # Fallback: use last stint in the list.
            for stint in reversed(data):
                if isinstance(stint, dict):
                    current = stint
                    break
        if current is None: return
        compound = current.get("compound", "")
        if not isinstance(compound, str): return
        new_type = _compound_type(compound)
        old_type = self._spell_type.get(num)
        self._compound[num] = compound
        if old_type is None:
            # First tyre info for this driver — initialize spell 1.
            self._spell_type[num] = new_type
            self._spell_idx[num] = 1
            self._spell_followed_wet[num] = False
            self._spell_best_ms[num] = None
            self._spell_best_ref[num] = []
            self._spell_best_pcts[num] = []
            self._spell_fast_laps[num] = []
        elif old_type != new_type:
            # Spell transition: dry↔wet.
            self._spell_type[num] = new_type
            self._spell_idx[num] = self._spell_idx.get(num, 0) + 1
            self._spell_followed_wet[num] = (old_type == "wet")
            self._spell_best_ms[num] = None
            self._spell_best_ref[num] = []
            self._spell_best_pcts[num] = []
            self._spell_fast_laps[num] = []
        # Same-type tyre change (e.g. soft→medium): no spell reset.

    def _is_wet_spell(self, num: str) -> bool:
        return self._spell_type.get(num) == "wet"

    # ── Event Routing ──

    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverStatus:"):
            num = topic.split(":", 1)[1]
            self._on_driver_status(num, data, clock_time)
        elif topic.startswith("driverTyres:"):
            num = topic.split(":", 1)[1]
            self._on_tyres_change(num, data, clock_time)
        elif topic == "position":
            if isinstance(data, dict):
                for num, coords in data.items():
                    if isinstance(coords, list) and len(coords) >= 3:
                        self._last_dist_pct[num] = coords[2]
        elif topic.startswith("driverLapTimes:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict):
                target = self._lap_actual_ms.setdefault(num, {})
                prev_max = max(target.keys(), default=0)
                for lap_str, time_str in data.items():
                    try:
                        lap = int(lap_str)
                    except (TypeError, ValueError):
                        continue
                    ms = _parse_time_ms(time_str)
                    if ms is not None:
                        target[lap] = ms
                new_max = max(target.keys(), default=0)
                # For each just-added lap with an LL value:
                #   1. Run _classify_on_lap_time to apply the new
                #      lap-time-driven classification rules (retroactive
                #      reclass, PUSH/LONG promotion, WET-spell override).
                #   2. Re-emit the classification — _start_lap from
                #      _handle_timing fires BEFORE this wildcard within
                #      the same TimingData event; its _emit_status was
                #      gated by lap_actual_ms presence and got skipped.
                if new_max > prev_max:
                    for lap in range(prev_max + 1, new_max + 1):
                        if lap in target:
                            self._classify_on_lap_time(num, lap, clock_time)

    # ── Lap-time-driven classification (SME 2026-06-03) ──

    def _classify_on_lap_time(self, num: str, lap: int, clock_time: datetime) -> None:
        """Apply the lap-time-driven classification rules when F1
        reports lap `lap`'s LastLapTime. See the SME spec:

          - Race laps: emit RACE (unless OUT/IN/STOP already set).
          - Wet spell: emit WET (unless OUT/IN/STOP already set).
          - Dry spell P/Q: PUSH initially; if not within-lap-COOL'd
            and not already locked, refine via retroactive reclass +
            PUSH/LONG cluster promotion.
        """
        if self._stopped.get(num):
            return
        cls = self._lap_history.get(num, {}).get(lap, "")
        # Locked: IN/STOP are irrevocable (except STOP→un-retire).
        if cls in ("IN", "STOP", "PIT"):
            self._reemit(num, lap, clock_time)
            return
        # Race: always RACE (unless OUT/IN/STOP).
        if self._is_race:
            if cls != "OUT":
                self._set_class(num, lap, "RACE", clock_time)
            else:
                self._reemit(num, lap, clock_time)
            return
        # Wet spell: emit WET (unless OUT).
        if self._is_wet_spell(num):
            if cls != "OUT":
                self._set_class(num, lap, "WET", clock_time)
            else:
                self._reemit(num, lap, clock_time)
            return
        # OUT laps stay OUT (= they can only be overridden by IN/STOP).
        if cls == "OUT":
            self._reemit(num, lap, clock_time)
            return
        # COOL laps stay COOL (= within-lap detection locked them).
        if cls == "COOL":
            self._reemit(num, lap, clock_time)
            return

        # Dry P/Q timed lap. Determine initial = PUSH.
        lap_ms = self._lap_actual_ms.get(num, {}).get(lap)
        if not isinstance(lap_ms, int):
            self._reemit(num, lap, clock_time)
            return

        # Absolute-delta COOL fallback (SME 2026-06-07): when the
        # speed-driven in-lap COOL detector silently fails (= telemetry
        # outage, speed reported as 0 for the whole lap), the lap stays
        # PUSH despite being obviously a cool-down by lap time. If THIS
        # lap is >=5 s slower than the driver's spell best, classify
        # COOL directly and bypass the PUSH/LONG promotion chain. Lap
        # 1 of a spell (= no spell_best yet) is unaffected.
        spell_best = self._spell_best_ms.get(num)
        if spell_best is not None and lap_ms - spell_best >= 5000:
            self._set_class(num, lap, "COOL", clock_time)
            # Drop this lap from any further fast-lap considerations.
            self._spell_fast_laps[num] = [
                (l, t) for l, t in self._spell_fast_laps.get(num, [])
                if l != lap
            ]
            # Neighbour resolution so prev lap's LONG status can collapse
            # back to PUSH when this no-longer-fast lap breaks the chain.
            self._resolve_push_long(num, lap - 1, clock_time)
            return

        # Update spell best if this lap is the new best.
        is_new_best = spell_best is None or lap_ms < spell_best
        if is_new_best:
            self._spell_best_ms[num] = lap_ms
            # Capture speed reference from this lap's CarData samples.
            speeds = self._current_lap_speeds.get(num, [])
            if len(speeds) >= 10:
                ref = sorted(speeds, key=lambda x: x[0])
                self._spell_best_ref[num] = ref
                self._spell_best_pcts[num] = [p for p, _ in ref]

        # Retroactive reclass — scan previous fast laps in current
        # spell, demote any 10s+ slower than this lap to COOL.
        followed_wet = self._spell_followed_wet.get(num, False)
        fast_laps = self._spell_fast_laps.setdefault(num, [])
        scope = fast_laps[-1:] if followed_wet else list(fast_laps)
        for prev_lap, prev_ms in scope:
            if prev_lap == lap: continue
            if prev_ms - lap_ms >= RETRO_RECLASS_THRESHOLD_MS:
                self._set_class(num, prev_lap, "COOL", clock_time)
                self._spell_fast_laps[num] = [
                    (l, t) for l, t in self._spell_fast_laps[num]
                    if l != prev_lap
                ]
                self._resolve_push_long(num, prev_lap - 1, clock_time)
                self._resolve_push_long(num, prev_lap + 1, clock_time)

        # Set this lap = PUSH (= initial fast classification).
        self._set_class(num, lap, "PUSH", clock_time)
        # Add to spell_fast_laps for future retroactive scans.
        self._spell_fast_laps[num] = [
            (l, t) for l, t in self._spell_fast_laps[num] if l != lap
        ]
        self._spell_fast_laps[num].append((lap, lap_ms))
        # PUSH/LONG cluster: promote consecutive fast laps to LONG.
        # ONLY for the just-completed lap and the one before it. We do
        # NOT promote lap+1 — the in-progress lap has no time yet, so
        # any classification beyond its default would be speculative.
        # When lap+1's own lap-time arrives, _resolve_push_long(lap+1)
        # will be called via this same path and the promotion happens
        # at the right moment with the right data.
        self._resolve_push_long(num, lap, clock_time)
        self._resolve_push_long(num, lap - 1, clock_time)

    # Classification priority — only LOWER or equal-priority transitions
    # are allowed. STOP > IN/PIT > OUT > COOL > PUSH/LONG/RACE/WET.
    _CLASS_RANK = {
        "STOP": 5,
        "IN": 4, "PIT": 4,
        "OUT": 3,
        "COOL": 2,
        "PUSH": 1, "LONG": 1, "RACE": 1, "WET": 1,
    }

    def _set_class(self, num: str, lap: int, status: str,
                   clock_time: datetime) -> None:
        """Set lap K's classification. Only emits if changed.
        Enforces priority hierarchy: lower-priority statuses cannot
        downgrade a higher-priority one."""
        if lap < 1: return
        cur = self._lap_history.get(num, {}).get(lap)
        if cur == status: return
        cur_rank = self._CLASS_RANK.get(cur, 0)
        new_rank = self._CLASS_RANK.get(status, 0)
        if cur_rank > new_rank:
            return
        self._classification[num] = status
        self._last_emitted_cls[num] = ""
        self._emit(num, lap, clock_time)

    def _reemit(self, num: str, lap: int, clock_time: datetime) -> None:
        """Re-emit current classification (= force a fresh emit so the
        new lap_actual_ms entry surfaces via the gate)."""
        cls = self._lap_history.get(num, {}).get(lap)
        if not cls: return
        saved = self._classification.get(num)
        self._classification[num] = cls
        self._last_emitted_cls[num] = ""
        self._emit(num, lap, clock_time)
        if saved is not None:
            self._classification[num] = saved

    def _resolve_push_long(self, num: str, lap: int,
                           clock_time: datetime) -> None:
        """Re-evaluate PUSH/LONG status for lap `lap` based on
        immediate neighbors. PUSH = both neighbors are slow
        (OUT/IN/COOL/PIT/STOP). LONG = at least one neighbor is fast
        (PUSH/LONG). Race/wet/qualifying laps are not affected.

        Per SME: there is no such thing as a LONG lap in qualifying
        conditions — all timed laps are PUSH attempts. The cluster
        promotion only applies to practice sessions.
        """
        if lap < 1: return
        if self._is_race or self._is_wet_spell(num) or self._is_qualifying:
            return
        cls = self._lap_history.get(num, {}).get(lap)
        if cls not in ("PUSH", "LONG"): return
        prev = self._lap_history.get(num, {}).get(lap - 1)
        nxt = self._lap_history.get(num, {}).get(lap + 1)
        has_fast = (prev in ("PUSH", "LONG")) or (nxt in ("PUSH", "LONG"))
        new = "LONG" if has_fast else "PUSH"
        if new != cls:
            self._classification[num] = new
            self._last_emitted_cls[num] = ""
            self._emit(num, lap, clock_time)

    def _handle_timing(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return
        for num, patch in lines.items():
            if not isinstance(patch, dict):
                continue

            # STOP detection: F1's Retired=True is the only reliable
            # "driver stopped on track" signal. SME 2026-06-03: STOP
            # is only set from timing data, not inferred from speed-
            # zero (which produces spurious STOPs during a spin-and-
            # resume). If Retired is later un-set, we revert STOP.
            retired = patch.get("Retired")
            if retired is True and not self._stopped.get(num):
                self._stopped[num] = True
                lap = self._cur_lap(num)
                if lap >= 1:
                    self._classification[num] = "STOP"
                    self._last_emitted_cls[num] = ""
                    self._emit(num, lap, clock_time)
                continue
            elif retired is False and self._stopped.get(num):
                # Un-retire: try to recover the prior classification by
                # rerunning the lap-time-driven logic. Best-effort.
                self._stopped[num] = False
                # Don't auto-revert here; subsequent events will
                # reclassify naturally (e.g. driverStatus TRACK fires
                # → _start_lap (OUT or TIMED)).

            # Race 2026: LastLapTime arrival is the lap-boundary signal
            # (NumberOfLaps no longer arrives per-driver).
            new_lap: Optional[int] = None
            if "NumberOfLaps" in patch:
                new_lap = int(patch["NumberOfLaps"])
            elif self._is_race:
                llt = patch.get("LastLapTime")
                if isinstance(llt, dict) and llt.get("Value"):
                    cur = self._timing_lap.get(num, 0)
                    new_lap = cur + 1 if cur > 0 else 2

            if new_lap is None:
                continue

            old_lap = self._timing_lap.get(num)
            self._timing_lap[num] = new_lap
            # NL bumped: use the LL-counter-based cur_lap rather than
            # NL value (F1 sometimes inserts extra NL bumps at pit-lane
            # crossings that the LL-counter ignores).
            if old_lap != new_lap:
                cur = self._cur_lap(num)
                if self._pending_out.pop(num, False):
                    self._collecting[num] = True
                    self._start_lap(num, cur, "OUT", clock_time)
                elif self._collecting.get(num) and old_lap is not None:
                    # The lap that just ended = the lap before cur.
                    self._finalize_lap(num, cur - 1, clock_time)
                    # Don't override OUT/IN classifications set earlier
                    # in this envelope (e.g. by a direct-emit OUT or PIT
                    # event whose dispatch ran before this _handle_timing).
                    if self._lap_history.get(num, {}).get(cur) not in ("OUT", "PIT", "IN"):
                        self._start_lap(num, cur, "TIMED", clock_time)

    # ── Driver Status ──

    def _cur_lap(self, num: str) -> int:
        """The lap currently being driven, per F1 NL convention
        (NL=K means currently on lap K). No placeholder.

        Uses _timing_lap which tracks the latest NumberOfLaps value
        for this driver. At session start (before NL=1) returns 1 as
        a safe default (= driver in pit, classified as PIT before
        first PitOut)."""
        return self._timing_lap.get(num) or 1

    def _on_driver_status(self, num: str, status: Any, clock_time: datetime) -> None:
        if status == "OUT":
            cur = self._cur_lap(num)
            # SME 2026-06-03: lap 1 in every P/Q session = OUT (= the
            # placeholder OUT lap, no lap_time). If lap 1 was emitted as
            # PIT at session start (driver in pit), reclassify to OUT
            # the moment driver exits pit for the first time.
            if (not self._is_race
                    and self._lap_history.get(num, {}).get(1) == "PIT"):
                saved_cls = self._classification.get(num)
                self._classification[num] = "OUT"
                self._last_emitted_cls[num] = ""
                self._emit(num, 1, clock_time)
                if saved_cls is not None:
                    self._classification[num] = saved_cls
            # Backfill: if the previous lap has no classification yet,
            # it must have been a pit-sit lap (e.g. driver in pit when
            # F1 sent the first non-empty LL for what FIA labels as
            # lap 2 = the long pit-sit lap). Mark it IN so the FIA
            # P=true matches.
            prev_lap = cur - 1
            if (prev_lap >= 2 and
                    self._lap_history.get(num, {}).get(prev_lap) not in
                    ("PIT", "IN", "OUT")):
                saved_classification = self._classification.get(num)
                saved_lap_type = self._lap_type.get(num)
                self._classification[num] = "PIT"
                self._lap_type[num] = "IN"
                self._last_emitted_cls[num] = ""  # force re-emit
                self._emit(num, prev_lap, clock_time)
                self._lap_type[num] = saved_lap_type
                self._classification[num] = saved_classification
            self._collecting[num] = True
            if self._is_race:
                # Race-mode pit exit: F1's status=OUT fires while the
                # driver is still in the LAP that includes the pit
                # visit (= lap N, the IN lap). Don't overwrite N's PIT
                # classification — set _pending_out so the next NL bump
                # marks lap N+1 as OUT (= the proper out lap). Without
                # this, lap N's PIT would flip to OUT and lap N+1 would
                # become RACE, losing both the IN and OUT classifications.
                self._pending_out[num] = True
            else:
                self._pending_out[num] = False
                self._start_lap(num, cur, "OUT", clock_time)
        elif status == "TRACK":
            if self._collecting.get(num):
                return
            self._collecting[num] = True
            is_out = self._pending_out.pop(num, False)
            prev_cls = self._prev_classification.get(num)
            if not is_out and (prev_cls is None or prev_cls in ("PIT", "IN")):
                is_out = True
            lap = self._cur_lap(num)
            self._start_lap(num, lap, "OUT" if is_out else "TIMED", clock_time)
        elif status == "PIT":
            # In-pit: classify current lap as PIT (= IN). Per SME this
            # also handles the OUT→IN reclassification: a lap that
            # started OUT becomes IN when the driver re-enters pit on
            # the same lap.
            #
            # Only EMIT lap_class for `lap` if F1 has already reported
            # its lap_time. Otherwise the PIT state is recorded in
            # _classification but no row is emitted yet; the
            # driverLapTimes wildcard handler will emit when the
            # lap_time arrives. This prevents orphan lap_class entries
            # for trailing IN laps where the session ends mid-lap and
            # F1 never reports a time (e.g. NOR Melbourne FP1 lap 8,
            # or Q1/Q2-end laps that don't complete).
            lap = self._cur_lap(num)
            if lap > 0:
                self._classification[num] = "PIT"
                self._lap_type[num] = "IN"
                # Record the lock in _lap_history (even though external
                # emit is deferred until the lap_time arrives). This
                # makes the PIT lock visible to _classify_on_lap_time's
                # locked-state check and prevents the lap-time-driven
                # logic from overwriting PIT with PUSH/COOL.
                if num not in self._lap_history:
                    self._lap_history[num] = {}
                self._lap_history[num][lap] = "PIT"
                self._prev_classification[num] = "PIT"
                # The previous timed lap may have been retroactively
                # promoted to LONG because THIS lap's default PUSH was
                # still its forward neighbour. Now that THIS lap is PIT
                # (= no longer fast), re-resolve the previous lap so
                # an unjustified LONG collapses back to PUSH. Must run
                # AFTER setting _lap_history[lap]=PIT so prev sees the
                # new state.
                self._resolve_push_long(num, lap - 1, clock_time)
            self._collecting[num] = False
            self._pending_out[num] = False

    # ── Lap Lifecycle ──

    def _start_lap(self, num: str, lap: int, lap_type: str, clock_time: datetime) -> None:
        self._lap_type[num] = lap_type
        self._slow_window[num] = []
        self._is_slow[num] = False
        self._lap_speeds[num] = []
        self._last_emitted_cls[num] = ""

        # Stamp the qualifying segment at lap-start so later
        # re-classifications can't drag the lap into the wrong segment
        # (used by the telemetry lap-list segment grouping).
        if self._is_qualifying:
            if num not in self._lap_segment:
                self._lap_segment[num] = {}
            if lap not in self._lap_segment[num]:
                self._lap_segment[num][lap] = self._current_qual_part

        if lap_type == "OUT":
            self._classification[num] = "OUT"
        elif self._is_race:
            self._classification[num] = "RACE"
        else:
            self._classification[num] = "PUSH"

        self._emit(num, lap, clock_time)

    def _finalize_lap(self, num: str, lap: int, clock_time: datetime) -> None:
        lap_type = self._lap_type.get(num, "TIMED")
        classification = self._classification.get(num, "PUSH")

        if lap_type == "OUT":
            self._prev_classification[num] = "OUT"
            return

        if not self._is_slow.get(num):
            avg_speed = self._current_avg_speed(num)
            classification = self._resolve_fast_classification(num, lap, avg_speed, clock_time)
            self._classification[num] = classification
            self._emit(num, lap, clock_time)
            self._maybe_update_best_ref(num)
            self._prev_fast_avg_speed[num] = avg_speed

        self._prev_classification[num] = classification

    def _current_avg_speed(self, num: str) -> float:
        speeds = self._lap_speeds.get(num, [])
        if not speeds:
            return 0.0
        return sum(s for _, s in speeds) / len(speeds)

    def _resolve_fast_classification(
        self, num: str, lap: int, avg_speed: float, clock_time: datetime
    ) -> str:
        if self._is_race:
            return "RACE"
        if self._is_qualifying:
            # Lap-time fallback: real-time slow detection needs a
            # reference best lap to compare against. On the FIRST flying
            # lap after OUT there's no reference yet, so a slow prep
            # lap (2-prep-lap routine) falsely sticks at PUSH. If we
            # already have a session-best time for this driver and the
            # just-completed lap is meaningfully slower, classify as
            # COOL instead.
            times = self._lap_actual_ms.get(num) or {}
            lap_ms = times.get(lap)
            if lap_ms and times:
                best_ms = min(t for t in times.values() if t)
                if lap_ms - best_ms > 2000:
                    return "COOL"
            return "PUSH"

        # Practice: consecutive fast → LONG; first fast after non-fast → PUSH
        prev = self._prev_classification.get(num)
        if prev in ("PUSH", "LONG"):
            # Exception: if prev was PUSH (first fast after out/slow) and this lap
            # is >10% faster, the previous lap was a slow warm-up — reclassify it
            # as COOL and keep this one as PUSH.
            prev_avg = self._prev_fast_avg_speed.get(num, 0)
            if prev == "PUSH" and prev_avg > 0 and avg_speed > prev_avg * 1.10:
                self._emit_status(num, lap - 1, "COOL", clock_time)
                self._prev_classification[num] = "COOL"
                return "PUSH"

            if prev == "PUSH":
                self._emit_status(num, lap - 1, "LONG", clock_time)
            return "LONG"
        return "PUSH"

    # ── Speed Analysis (during lap) ──

    def _handle_car_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        entries = data.get("Entries")
        if not isinstance(entries, list):
            return

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cars = entry.get("Cars")
            if not isinstance(cars, dict):
                continue

            for num, car in cars.items():
                if not isinstance(car, dict):
                    continue
                try:
                    if int(num) > 99:
                        continue
                except ValueError:
                    continue
                ch = car.get("Channels")
                if not isinstance(ch, dict):
                    continue
                if not self._collecting.get(num):
                    continue

                dist_pct = self._last_dist_pct.get(num)
                if dist_pct is None:
                    continue

                speed = ch.get("2", 0)
                self._accumulate_and_classify(num, dist_pct, speed, clock_time)

    def _accumulate_and_classify(
        self, num: str, dist_pct: float, speed: float, clock_time: datetime
    ) -> None:
        if num not in self._lap_speeds:
            self._lap_speeds[num] = []
        self._lap_speeds[num].append((dist_pct, speed))

        # Slow detection only runs on TIMED laps. OUT and IN laps are
        # locked classifications per SME: "OUT laps never get
        # reclassified, unless driver returns to the pits in the same
        # lap (in which case OUT → IN); IN laps stay IN."
        if self._lap_type.get(num) not in ("TIMED",):
            return
        if self._is_race:
            return
        if self._is_slow.get(num):
            return
        if dist_pct > SLOW_CHECK_MAX_PCT:
            return

        ref_speed = self._interp_best(num, dist_pct)
        if ref_speed is None:
            return

        t = clock_time.timestamp()
        is_slow_pt = (ref_speed - speed) > SLOW_THRESHOLD_KMH

        window = self._slow_window.get(num)
        if window is None:
            window = []
            self._slow_window[num] = window
        window.append((t, is_slow_pt))

        # Trim entries older than 2x window (keep buffer so span check works)
        trim_cutoff = t - SLOW_WINDOW_S * 2
        while window and window[0][0] < trim_cutoff:
            window.pop(0)

        # Need at least SLOW_WINDOW_S of accumulated data
        if not window or (t - window[0][0]) < SLOW_WINDOW_S:
            return

        # Check the last SLOW_WINDOW_S seconds only
        check_start = t - SLOW_WINDOW_S
        check_pts = [(ts, s) for ts, s in window if ts >= check_start]
        if not check_pts:
            return
        total = len(check_pts)
        slow_count = sum(1 for _, s in check_pts if s)
        if slow_count >= total * (1.0 - SLOW_OUTLIER_RATIO):
            self._is_slow[num] = True
            lap = self._cur_lap(num)
            self._classification[num] = "COOL"
            self._emit(num, lap, clock_time)

    # ── Best-Lap Reference ──

    def _maybe_update_best_ref(self, num: str) -> None:
        speeds = self._lap_speeds.get(num, [])
        if len(speeds) < 10:
            return
        avg = sum(s for _, s in speeds) / len(speeds)
        if avg > self._best_avg_speed.get(num, 0):
            self._best_avg_speed[num] = avg
            ref = sorted(speeds, key=lambda x: x[0])
            self._best_ref[num] = ref
            self._best_ref_pcts[num] = [p for p, _ in ref]

    def _interp_best(self, num: str, dist_pct: float) -> Optional[float]:
        ref = self._best_ref.get(num)
        pcts = self._best_ref_pcts.get(num)
        if not ref or not pcts:
            return None
        if dist_pct < pcts[0] or dist_pct > pcts[-1]:
            return None

        i = bisect_left(pcts, dist_pct)
        if i == 0:
            return ref[0][1]
        d0, s0 = ref[i - 1]
        d1, s1 = ref[i]
        if d1 == d0:
            return s0
        t = (dist_pct - d0) / (d1 - d0)
        return s0 + t * (s1 - s0)

    # ── Emission ──

    def _emit(self, num: str, lap: int, clock_time: datetime) -> None:
        cls = self._classification.get(num, "")
        if not cls or self._last_emitted_cls.get(num) == cls:
            return
        self._last_emitted_cls[num] = cls
        self._emit_status(num, lap, cls, clock_time)

    def _emit_status(self, num: str, lap: int, status: str, clock_time: datetime) -> None:
        # Per-(driver, lap) dedup: if we've already sent THIS status
        # for THIS lap, skip. Without this, _set_class/_reemit reset
        # the per-driver gate and force-emit even when no real change
        # happened (= the "duplicate emits at same offset" the SME spotted).
        key = (num, lap)
        if self._last_emitted_per_lap.get(key) == status:
            return
        self._last_emitted_per_lap[key] = status

        # Always update internal _lap_history (= used by _handle_timing's
        # protection check `_lap_history[cur] not in (OUT,PIT,IN)` to
        # avoid overwriting an OUT/PIT classification with TIMED on the
        # next NL bump).
        if num not in self._lap_history:
            self._lap_history[num] = {}
        self._lap_history[num][lap] = status
        valid_classifications = (
            "OUT", "PIT", "IN", "STOP",
            "PUSH", "COOL", "LONG", "RACE", "WET",
        )
        if status not in valid_classifications:
            return

        # Defer the bus emit. Park the header (lap, status) — when the
        # outer handler finishes calling _flush_dirty(), one message
        # per driver fires with the freshly-mutated _lap_history map.
        # Prefer the HIGHEST lap as the header (= a new-lap start beats
        # a retroactive reclassification of an older lap).
        # Body deferred to _flush_dirty (called by every top-level
        # handler). Park the header (lap, status); the batched emit
        # fires there.
        cur = self._dirty_header.get(num)
        if cur is None or lap >= cur[0]:
            self._dirty_header[num] = (lap, status)

    def _flush_dirty(self, clock_time: datetime) -> None:
        """Emit one batched message per dirty driver.

        Called at the END of every top-level bus handler. By that point
        all state mutations triggered by the inbound event have been
        applied to ``_lap_history`` synchronously; the header (lap,
        status) is the highest-lap change in this batch (= a new-lap
        start outranks any retroactive reclassification of an older
        lap). The emitted ``laps`` map carries every known per-lap
        classification including the in-progress lap.
        """
        if not self._dirty_header:
            return
        # Snapshot + clear FIRST so the recursive _flush_after hits from
        # bus.emit's downstream listeners (= same wildcard wrapper) see
        # an empty dirty set and don't loop. RuntimeError without this.
        pending = list(self._dirty_header.items())
        self._dirty_header.clear()
        for num, (lap, status) in pending:
            if self._is_qualifying:
                seg_map = self._lap_segment.setdefault(num, {})
                seg = seg_map.get(lap, self._current_qual_part)
                lap_segments = {str(k): v for k, v in seg_map.items()}
            else:
                seg = 0
                lap_segments = {}
            self._bus.emit(f"lapClassification:{num}", {
                "lap": lap, "status": status,
                "segment": seg,
                "laps": {str(k): v for k, v in self._lap_history.get(num, {}).items()},
                "lapSegments": lap_segments,
            }, clock_time)
            # Internal topic for run_plan + telemetry_display processors
            # (current-lap status). Emitted alongside the client-facing
            # lapClassification:{num} topic.
            self._bus.emit("~lap-status", {
                "driverNum": num, "lapNumber": lap, "status": status,
            }, clock_time)

    def finalize_session(self, last_clock: datetime) -> None:
        """At session end: ensure every lap from 1 to NL_max has a
        classification entry AND ensure each driver's last lap_class
        emit includes ALL laps (= flush the deferred PIT lap-class
        emissions for trailing in-progress laps where F1 never
        published a lap_time).
        """
        # Drain any per-handler batch that wasn't flushed yet (= edge
        # case at session boundary where the wrapping handler never
        # returned).
        self._flush_dirty(last_clock)
        # — original docstring continues —
        """

        The PIT handler records `_lap_history[num][lap] = "PIT"` but
        defers the bus emit until F1 reports the lap_time. For the
        trailing in-progress lap (= driver entered pit at session
        end), the lap_time never arrives, so the PIT row never reaches
        the bus. This flush forces it out."""
        for num, max_nl in self._timing_lap.items():
            if not max_nl or max_nl < 1:
                continue
            hist = self._lap_history.setdefault(num, {})
            # Fill any missing slots 1..max_nl with IN as a safe default.
            for ln in range(1, max_nl + 1):
                if ln not in hist:
                    hist[ln] = "IN"
            # Emit a final lap_class with the full history, including
            # the last lap (which the regular emit path may have
            # deferred). Bypass the keep() filter for this final pass
            # so trailing in-progress laps are visible.
            payload_laps = {str(k): v for k, v in hist.items()}
            if self._is_qualifying:
                seg = {str(k): v for k, v in self._lap_segment.get(num, {}).items()
                       if int(k) in hist}
            else:
                seg = {}
            self._bus.emit(f"lapClassification:{num}", {
                "lap": max_nl,
                "status": hist[max_nl],
                "segment": (self._lap_segment.get(num, {}).get(max_nl, 0)
                            if self._is_qualifying else 0),
                "laps": payload_laps,
                "lapSegments": seg,
            }, last_clock)
