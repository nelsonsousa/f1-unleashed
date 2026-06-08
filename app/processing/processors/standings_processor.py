"""
Standings Processor — driver standings for all session types.

Subscribes to: DriverList, TimingData, TimingAppData, SessionData (qualifying),
               RaceControlMessages, SessionInfo, LapCount (race)
Emits: display:standings, display:qualifying-segment

Ports the standings logic from standings_practice.js, standings_qualifying.js,
and standings_race.js.
"""

import re
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# Fallback team colors (2026 season) when DriverList doesn't provide TeamColour
TEAM_COLORS = {
    "1": "#ff8000", "81": "#ff8000",       # McLaren
    "3": "#1e3d7b", "6": "#1e3d7b",        # Red Bull
    "16": "#e8002d", "44": "#e8002d",      # Ferrari
    "12": "#00d4be", "63": "#00d4be",      # Mercedes
    "14": "#1a7a5a", "18": "#1a7a5a",      # Aston Martin
    "10": "#00a1e8", "43": "#00a1e8",      # Alpine
    "23": "#0f4c91", "55": "#0f4c91",      # Williams
    "30": "#2d826d", "41": "#2d826d",      # Racing Bulls
    "31": "#ffffff", "87": "#ffffff",      # Haas
    "5": "#990000", "27": "#990000",       # Audi
    "11": "#6e6e70", "77": "#6e6e70",      # Cadillac
}
DEFAULT_CAR_COLOR = "#888888"

HIGHLIGHT_DURATION_S = 5.0

# Practice / qualifying session end: F1 broadcasts this RCM when the
# first car crosses the line under the chequered. The named car is the
# first to finish; the rest finish as they pit or cross S/F afterwards.
_FIRST_FLAG_RX = re.compile(r"FIRST CAR TO TAKE THE FLAG.*?CAR\s+(\d+)", re.I)


def _parse_lap_time_ms(time_str: Optional[str]) -> Optional[float]:
    """Parse lap time like '1:23.456' to milliseconds."""
    if not time_str or not isinstance(time_str, str):
        return None
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60000 + seconds * 1000
        if len(parts) == 1:
            return float(parts[0]) * 1000
    except (ValueError, IndexError):
        pass
    return None


def _format_gap(diff_ms: float) -> str:
    """Format millisecond difference as '+X.XXX'."""
    return f"+{diff_ms / 1000:.3f}"


def _is_wet_compound(compound: Optional[str]) -> bool:
    if not compound:
        return False
    return compound.upper() in ("INTERMEDIATE", "WET")


def _create_driver(num: str) -> dict[str, Any]:
    """Create a new driver state dict."""
    return {
        "num": num,
        "tla": num,
        "team": "",
        "color": TEAM_COLORS.get(num, DEFAULT_CAR_COLOR),
        "position": 99,
        # Practice / Qualifying
        "bestLap": None,
        "bestLapPersonal": False,
        "bestLapOverall": False,
        "bestLapTyreCompound": None,
        "bestLapTyreIsNew": None,
        "currentTyreCompound": None,
        "currentTyreIsNew": None,
        "gap": None,
        # Qualifying: set True on a segment transition (Q1→Q2, Q2→Q3) for
        # advancing drivers, so F1's TimingData BestLapTime / GapToLeader
        # from the prior segment aren't re-written into the driver after
        # the local clear. Reset when F1 sends a fresh LastLapTime (lap
        # completed in the new segment).
        "awaitNewLapForBest": False,
        # Qualifying
        "highlight": None,
        "highlightStart": None,
        # Race
        "interval": None,
        "inPit": False,
        "retired": False,
        "stopped": False,
        "stints": [],
        "lastLapNumber": 0,
        "underInvestigation": False,
        "penalty": None,
        "trackLimitsWarning": False,
        "blackFlag": False,
        # FIA-STEWARDS state machine (= 2026-06-06 spec).
        "penKind": None,        # investigation / deferred / noted / 5s / 10s / dt / sg
        "penReason": None,      # e.g. "STARTING PROCEDURE INFRINGEMENT"
        "penIncident": None,    # tuple-key for multi-driver incident resolution
        # Flag indicators (= separate from the PEN badge).
        "trackLimitsFlag": False,    # waving black-and-white-flag SVG
        "blueFlagUntilMs": None,     # session-clock ms when blue-flag expires
        # Race-finished flag — true once the driver has crossed S/F
        # following (or coincident with) the chequered flag. Used to
        # render the chequered badge in standings_race.
        "finished": False,
    }


class StandingsProcessor(Processor):
    """Computes driver standings for practice, qualifying, and race sessions."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict[str, Any]] = {}
        self._is_race = session_type == "race"
        self._is_qualifying = session_type == "qualifying"

        # Qualifying
        self._qualifying_segment: Optional[str] = None
        self._eliminated: set[str] = set()
        self._is_sprint_quali = False

        # Race
        self._current_lap: Optional[int] = None
        self._total_laps: Optional[int] = None
        self._is_sprint = False
        self._last_rcm_key = -1
        # Race-finish tracking: when the chequered flag is shown the
        # leader has just crossed S/F → finished. Every other driver
        # crosses S/F on the lap that started with the chequered to
        # complete the race. Snap each driver's `finished` flag
        # individually as their next NumberOfLaps increment arrives.
        self._chequered_seen = False
        # Per-driver lap count captured at the moment chequered fired
        # (the lap they're racing to S/F to complete).
        self._lap_at_chequered: dict[str, int] = {}
        # Practice/qualifying: car named in "FIRST CAR TO TAKE THE FLAG".
        # (COOL-lap+delta finish rule deliberately parked — SME 2026-06-07.)
        self._first_flag_driver: Optional[str] = None

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("TimingAppData", self._handle_timing_app_data)
        # Race: latch when the chequered flag is shown, so we can flip
        # drivers' `finished` flag as each crosses S/F afterwards.
        if self._is_race:
            self._bus.on("trackStatus", self._handle_track_status_topic)

        if self._is_qualifying:
            self._bus.on("SessionData", self._handle_session_data)
            # Listen to the SessionDataProcessor's qualifyingPart topic
            # too — the raw SessionData fan-out can omit QualifyingPart
            # from `entries[-1]` for the very first arrival (often only
            # QualifyingPart=1 in an earlier entry), leaving _qualifying_segment
            # = None through Q1 and breaking the gap-to-cutoff logic.
            self._bus.on("qualifyingPart", self._handle_qualifying_part_topic)

        if self._is_race:
            self._bus.on("RaceControlMessages", self._handle_rcm)
            self._bus.on("SessionInfo", self._handle_session_info)
            self._bus.on("LapCount", self._handle_lap_count)
        else:
            # Practice / qualifying: drive per-driver `finished` (the
            # chequered marker) from the "FIRST CAR TO TAKE THE FLAG" RCM.
            self._bus.on("RaceControlMessages", self._handle_rcm_finish)

    def _get_or_create(self, num: str) -> dict[str, Any]:
        if num not in self._drivers:
            self._drivers[num] = _create_driver(num)
        return self._drivers[num]

    # ── Handlers ──

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, info in data.items():
            if not isinstance(info, dict):
                continue
            driver = self._get_or_create(num)
            if info.get("Tla"):
                driver["tla"] = info["Tla"]
            if info.get("TeamName"):
                driver["team"] = info["TeamName"]
            if info.get("TeamColour"):
                color = info["TeamColour"]
                if not color.startswith("#"):
                    color = f"#{color}"
                driver["color"] = color
        self._emit_standings(clock_time)

    def _handle_timing_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        for num, timing in lines.items():
            if not isinstance(timing, dict):
                continue
            driver = self._get_or_create(num)

            # Position
            pos = timing.get("Position")
            if pos is not None:
                try:
                    driver["position"] = int(pos)
                except (ValueError, TypeError):
                    pass

            # A fresh NumberOfLaps INCREMENT (not a resend) means the
            # driver has just completed a lap — that ends the post-
            # segment-change blackout on bestLap / gap updates from
            # F1's TimingData. Using NumberOfLaps avoids the trap of
            # F1 re-sending LastLapTime as a snapshot with the prior
            # segment's value, which would clear the blackout and let
            # the stale value back in.
            new_laps_raw = timing.get("NumberOfLaps")
            if new_laps_raw is not None:
                try:
                    nl = int(new_laps_raw)
                    if nl > driver["lastLapNumber"]:
                        driver["awaitNewLapForBest"] = False
                except (ValueError, TypeError):
                    pass

            # Best lap (all session types). Skip while awaiting the
            # driver's first lap in a new qualifying segment so F1 can't
            # re-write the prior segment's value back into the driver.
            # `awaitNewLapForBest` never flips true outside qualifying,
            # so race + practice see this every TimingData arrival.
            if not driver["awaitNewLapForBest"]:
                self._update_best_lap(driver, timing, clock_time)

            # Gap to leader (also gated by the segment-change blackout).
            gap = timing.get("GapToLeader")
            if gap is not None and not driver["awaitNewLapForBest"]:
                driver["gap"] = gap.get("Value") if isinstance(gap, dict) else str(gap)

            # Practice: Stats.TimeDiffToFastest overrides gap
            if not self._is_race and not self._is_qualifying:
                stats = timing.get("Stats")
                if isinstance(stats, dict) and stats.get("TimeDiffToFastest"):
                    driver["gap"] = stats["TimeDiffToFastest"]

            # Race-specific fields
            if self._is_race:
                self._update_race_timing(driver, timing)
            else:
                self._update_finish_pq(driver, timing)

        self._expire_highlights(clock_time)
        self._emit_standings(clock_time)

    def _update_best_lap(
        self, driver: dict, timing: dict, clock_time: datetime
    ) -> None:
        """Update best lap time from TimingData."""
        blt = timing.get("BestLapTime")
        if blt is None:
            return

        new_value = None
        if isinstance(blt, str):
            new_value = blt
        elif isinstance(blt, dict):
            if blt.get("Value"):
                new_value = blt["Value"]
            if "PersonalFastest" in blt:
                driver["bestLapPersonal"] = blt["PersonalFastest"] is True
            if "OverallFastest" in blt:
                driver["bestLapOverall"] = blt["OverallFastest"] is True

                # Demote any other driver currently flagged as overall
                # best — F1 doesn't always send the paired OverallFastest=
                # false to the prior holder, so do it ourselves. Keep
                # their personal-best flag (still their fastest).
                if blt["OverallFastest"] is True:
                    for other in self._drivers.values():
                        if other["num"] == driver["num"]:
                            continue
                        if other["bestLapOverall"]:
                            other["bestLapOverall"] = False
                            other["bestLapPersonal"] = True

                # Flag-only upgrade: promote green highlight to purple
                if (blt["OverallFastest"] is True
                        and new_value is None
                        and driver["highlight"] == "green"
                        and not self._skip_animations):
                    driver["highlight"] = "purple"

        if new_value and new_value != driver["bestLap"]:
            driver["bestLap"] = new_value
            # Capture current tyre as best-lap tyre
            if driver["currentTyreCompound"]:
                driver["bestLapTyreCompound"] = driver["currentTyreCompound"]
                driver["bestLapTyreIsNew"] = driver["currentTyreIsNew"]

            # Set highlight (qualifying only)
            if self._is_qualifying and not self._skip_animations:
                is_overall = driver["bestLapOverall"]
                if not is_overall and driver["bestLapPersonal"]:
                    # Double-check: is this actually the overall best?
                    new_ms = _parse_lap_time_ms(new_value)
                    if new_ms is not None:
                        is_overall = all(
                            _parse_lap_time_ms(d["bestLap"]) is None
                            or _parse_lap_time_ms(d["bestLap"]) >= new_ms
                            for d in self._drivers.values()
                            if d["num"] != driver["num"]
                        )
                driver["highlight"] = "purple" if is_overall else "green"
                driver["highlightStart"] = clock_time

    def _update_race_timing(self, driver: dict, timing: dict) -> None:
        """Update race-specific fields from TimingData."""
        # Interval to car ahead
        interval = timing.get("IntervalToPositionAhead")
        if interval is not None:
            driver["interval"] = (
                interval.get("Value") if isinstance(interval, dict) else str(interval)
            )

        # Status flags
        if "InPit" in timing:
            driver["inPit"] = timing["InPit"] is True
        if "Retired" in timing:
            was = driver.get("retired") or False
            driver["retired"] = timing["Retired"] is True
            if driver["retired"] and not was:
                driver["retiredOnLap"] = driver.get("lastLapNumber") or 0
        if "Stopped" in timing:
            was = driver.get("stopped") or False
            driver["stopped"] = timing["Stopped"] is True
            if driver["stopped"] and not was:
                driver["retiredOnLap"] = driver.get("lastLapNumber") or 0

        # Lap tracking for stint lap count
        n_laps = timing.get("NumberOfLaps")
        if n_laps is not None:
            try:
                new_laps = int(n_laps)
            except (ValueError, TypeError):
                return
            if new_laps > driver["lastLapNumber"]:
                # Increment current stint local lap count
                stint = self._get_current_stint(driver)
                if stint:
                    stint["localLapCount"] += new_laps - driver["lastLapNumber"]
                driver["lastLapNumber"] = new_laps
                # If chequered has been shown and this driver is racing
                # to S/F to complete their final lap, this S/F crossing
                # = race finish for them. (The leader was already
                # marked finished at the chequered moment — handled in
                # _handle_track_status_topic.)
                if (self._chequered_seen
                        and not driver["finished"]
                        and driver["num"] in self._lap_at_chequered):
                    driver["finished"] = True

    def _handle_timing_app_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        for num, app_data in lines.items():
            if not isinstance(app_data, dict):
                continue
            stints = app_data.get("Stints")
            if not stints or not isinstance(stints, dict):
                continue

            driver = self._get_or_create(num)

            if self._is_race:
                self._update_race_stints(driver, stints)
            else:
                self._update_practice_tyre(driver, stints)

        self._emit_standings(clock_time)

    def _update_practice_tyre(self, driver: dict, stints: dict) -> None:
        """Update current tyre from the last stint (practice/qualifying)."""
        latest_compound = None
        latest_new = None
        for stint_info in stints.values():
            if isinstance(stint_info, dict):
                if stint_info.get("Compound"):
                    latest_compound = stint_info["Compound"]
                if "New" in stint_info:
                    val = stint_info["New"]
                    latest_new = val is True or val == "true"

        if latest_compound:
            driver["currentTyreCompound"] = latest_compound
        if latest_new is not None:
            driver["currentTyreIsNew"] = latest_new

        # Backfill best-lap tyre if missing
        if (driver["bestLap"]
                and not driver["bestLapTyreCompound"]
                and driver["currentTyreCompound"]):
            driver["bestLapTyreCompound"] = driver["currentTyreCompound"]
            driver["bestLapTyreIsNew"] = driver["currentTyreIsNew"]

    def _update_race_stints(self, driver: dict, stints_data: dict) -> None:
        """Update stint array from TimingAppData (race)."""
        for idx_str, stint_info in stints_data.items():
            if not isinstance(stint_info, dict):
                continue
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue

            # Ensure stints array is large enough
            while len(driver["stints"]) <= idx:
                driver["stints"].append({
                    "compound": None,
                    "isNew": None,
                    "totalLaps": 0,
                    "tyresNotChanged": False,
                    "localLapCount": 0,
                })

            stint = driver["stints"][idx]
            if stint_info.get("Compound"):
                stint["compound"] = stint_info["Compound"]
            if "New" in stint_info and stint["isNew"] is None:
                val = stint_info["New"]
                stint["isNew"] = val is True or val == "true"
            if "TotalLaps" in stint_info:
                try:
                    stint["totalLaps"] = int(stint_info["TotalLaps"])
                    stint["localLapCount"] = stint["totalLaps"]
                except (ValueError, TypeError):
                    pass
            if "TyresNotChanged" in stint_info:
                stint["tyresNotChanged"] = stint_info["TyresNotChanged"] in (
                    True, "1", 1
                )

    def _handle_qualifying_part_topic(self, data: Any, clock_time: datetime) -> None:
        """qualifyingPart topic (already an int, emitted by SessionDataProcessor).

        Only 1/2/3 are real segments — QualifyingPart=0 means qualifying
        hasn't started yet (or has been reset post-session), ignore.
        """
        if isinstance(data, int) and 1 <= data <= 3:
            self._apply_qualifying_segment(data, clock_time)

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        """Qualifying: detect segment transitions (Q1→Q2→Q3) from the
        raw SessionData fan-out. Scan ALL entries (not just the last) —
        F1 often only includes QualifyingPart in the entry that
        announced the transition, leaving later entries without it.
        """
        if not isinstance(data, dict):
            return
        series = data.get("Series")
        if not series or not isinstance(series, dict):
            return

        q_part: Optional[int] = None
        for entry in series.values():
            v = entry.get("QualifyingPart") if isinstance(entry, dict) else None
            if isinstance(v, int) and 1 <= v <= 3:
                if q_part is None or v > q_part:
                    q_part = v
        if q_part is None:
            return

        self._apply_qualifying_segment(q_part, clock_time)

    def _apply_qualifying_segment(self, q_part: int, clock_time: datetime) -> None:
        prefix = "S" if self._is_sprint_quali else ""
        new_segment = f"{prefix}Q{q_part}"

        if new_segment != self._qualifying_segment:
            old_segment = self._qualifying_segment
            self._qualifying_segment = new_segment

            if old_segment is not None:
                # Each Q segment has its own chequered — wipe the prior
                # segment's finished/flag state so it doesn't carry over.
                for d in self._drivers.values():
                    d["finished"] = False
                self._chequered_seen = False
                self._lap_at_chequered = {}
                self._first_flag_driver = None
                self._clear_laps_for_segment(new_segment)
                self._bus.emit("display:qualifying-segment", {
                    "segment": new_segment,
                    "eliminatedDrivers": list(self._eliminated),
                    "isSprintQuali": self._is_sprint_quali,
                }, clock_time)

            self._emit_standings(clock_time)

    def _handle_track_status_topic(self, data: Any, clock_time: datetime) -> None:
        """Race only: latch chequered moment + mark leader as finished.

        When chequered fires:
          - the leader (P1) has just crossed S/F → mark finished now.
          - every other driver is on the lap they need to complete to
            finish — snapshot their current lap so the next NumberOfLaps
            increment can flip their `finished` flag.
        """
        if not isinstance(data, dict) or data.get("status") != "finished" \
                or self._chequered_seen:
            return
        self._chequered_seen = True

        sorted_drivers = sorted(
            [d for d in self._drivers.values() if d["position"] < 99],
            key=lambda d: d["position"],
        )
        if not sorted_drivers:
            self._emit_standings(clock_time)
            return

        leader = sorted_drivers[0]
        leader["finished"] = True

        # All other drivers: snapshot the lap they were on when chequered
        # arrived. _update_race_timing will flip `finished` for each as
        # their next S/F crossing arrives.
        for d in sorted_drivers[1:]:
            self._lap_at_chequered[d["num"]] = d["lastLapNumber"]

        self._emit_standings(clock_time)

    # ── Practice / Qualifying finish ("chequered" marker) ──

    def _handle_rcm_finish(self, data: Any, clock_time: datetime) -> None:
        """Practice/qualifying: the `FIRST CAR TO TAKE THE FLAG - CAR N`
        RCM ends the session. Mark car N finished, snapshot everyone
        else's lap for S/F-crossing detection, and finish any car already
        in the pits. Only the first such message matters (per Q segment —
        reset on segment change in `_apply_qualifying_segment`).
        """
        if self._chequered_seen:
            return
        if not isinstance(data, dict):
            return
        messages = data.get("Messages")
        if isinstance(messages, dict):
            items = list(messages.values())
        elif isinstance(messages, list):
            items = messages
        else:
            return
        for msg in items:
            if not isinstance(msg, dict):
                continue
            m = _FIRST_FLAG_RX.search(msg.get("Message", "") or "")
            if m:
                self._on_first_flag(m.group(1), clock_time)
                break

    def _on_first_flag(self, num: str, clock_time: datetime) -> None:
        self._chequered_seen = True
        self._first_flag_driver = num
        first = self._drivers.get(num)
        if first:
            first["finished"] = True
        for d in self._drivers.values():
            if d["num"] == num:
                continue
            # Snapshot the lap each driver is on so their next S/F
            # crossing flips `finished` (handled in _update_finish_pq).
            self._lap_at_chequered[d["num"]] = d["lastLapNumber"]
            # Any car already in the pits at the flag is done.
            if d.get("inPit"):
                d["finished"] = True
        self._emit_standings(clock_time)

    def _update_finish_pq(self, driver: dict, timing: dict) -> None:
        """Practice/qualifying per-TimingData finish tracking. Keeps
        `inPit` + `lastLapNumber` current and, once the flag has fallen,
        finishes a driver when they pit or cross S/F.
        """
        if "InPit" in timing:
            driver["inPit"] = timing["InPit"] is True

        crossed = False
        n_laps = timing.get("NumberOfLaps")
        if n_laps is not None:
            try:
                nl = int(n_laps)
            except (ValueError, TypeError):
                nl = None
            if nl is not None and nl > driver["lastLapNumber"]:
                driver["lastLapNumber"] = nl
                crossed = True

        if not self._chequered_seen or driver["finished"]:
            return
        if driver["inPit"]:
            driver["finished"] = True
        elif crossed and driver["num"] in self._lap_at_chequered:
            driver["finished"] = True

    def _clear_laps_for_segment(self, new_segment: str) -> None:
        """Clear lap times for advancing drivers, mark eliminated."""
        sorted_drivers = sorted(
            [d for d in self._drivers.values() if d["position"] < 99],
            key=lambda d: d["position"],
        )
        driver_count = len(sorted_drivers) + len(self._eliminated)

        if new_segment in ("Q2", "SQ2"):
            knockout_pos = 16 if driver_count > 20 else 15
        elif new_segment in ("Q3", "SQ3"):
            knockout_pos = 10
        else:
            return

        for d in sorted_drivers:
            if d["position"] <= knockout_pos:
                # Advancing: clear lap times. awaitNewLapForBest blocks
                # F1 from immediately re-writing the prior segment's
                # BestLapTime / GapToLeader back into the driver — held
                # until the driver completes their first lap in the new
                # segment (see _handle_timing_data).
                d["bestLap"] = None
                d["bestLapPersonal"] = False
                d["bestLapOverall"] = False
                d["bestLapTyreCompound"] = None
                d["bestLapTyreIsNew"] = None
                d["gap"] = None
                d["highlight"] = None
                d["highlightStart"] = None
                d["awaitNewLapForBest"] = True
            else:
                # Eliminated
                self._eliminated.add(d["num"])

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        """Race: detect sprint."""
        if not isinstance(data, dict):
            return
        name = data.get("Name") or ""
        if name == "Sprint":
            self._is_sprint = True

        # Also detect sprint qualifying for badge
        lower = name.lower()
        if "sprint qualifying" in lower or "sprint shootout" in lower:
            self._is_sprint_quali = True

    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        """Race: update lap count."""
        if not isinstance(data, dict):
            return
        if "CurrentLap" in data:
            try:
                self._current_lap = int(data["CurrentLap"])
            except (ValueError, TypeError):
                pass
        if "TotalLaps" in data:
            try:
                self._total_laps = int(data["TotalLaps"])
            except (ValueError, TypeError):
                pass
        self._emit_standings(clock_time)

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        """Race: parse race control messages for penalties."""
        if not isinstance(data, dict):
            return
        messages = data.get("Messages") or data
        if not isinstance(messages, dict):
            return

        for key, msg in messages.items():
            if not isinstance(msg, dict):
                continue

            idx = None
            try:
                idx = int(key)
            except (ValueError, TypeError):
                pass
            if idx is not None and idx <= self._last_rcm_key:
                continue
            if idx is not None:
                self._last_rcm_key = idx

            self._process_rcm(msg)

        self._emit_standings(clock_time)

    # ── FIA STEWARDS state machine (= 2026-06-06 spec) ─────────────────

    _CAR_RX = re.compile(r"CAR\s*(\d+)", re.I)
    _TIME_PEN_RX = re.compile(r"(\d+)\s*SECOND(?:S)?\s*(TIME\s*PENALTY|PENALTY)", re.I)

    @staticmethod
    def _extract_cars(text: str) -> list[str]:
        """All car numbers mentioned in an RCM, in order, deduped."""
        seen: list[str] = []
        for m in StandingsProcessor._CAR_RX.finditer(text):
            n = m.group(1)
            if n not in seen:
                seen.append(n)
        return seen

    @staticmethod
    def _extract_reason(text: str) -> Optional[str]:
        """The trailing infringement clause after the last ' - ' separator."""
        if " - " not in text:
            return None
        tail = text.rsplit(" - ", 1)[-1].strip()
        # Strip a trailing timestamp like "(16:47:57)".
        tail = re.sub(r"\s*\(\d+:\d+:\d+\)\s*$", "", tail)
        return tail or None

    def _clear_pen(self, driver: dict, *, also_track_limits: bool = False) -> None:
        driver["penKind"] = None
        driver["penReason"] = None
        driver["penIncident"] = None
        driver["underInvestigation"] = False
        driver["penalty"] = None
        if also_track_limits:
            driver["trackLimitsFlag"] = False
            driver["trackLimitsWarning"] = False

    def _set_pen(self, driver: dict, kind: str, reason: Optional[str],
                 incident: Optional[tuple]) -> None:
        driver["penKind"] = kind
        driver["penReason"] = reason
        driver["penIncident"] = incident
        # Mirror onto legacy fields so unchanged code keeps working.
        driver["underInvestigation"] = (kind == "investigation")
        if kind in ("5s", "10s", "dt", "sg") or (kind and kind.endswith("s")):
            driver["penalty"] = {"type": kind, "served": False}
        elif kind not in ("investigation",):
            driver["penalty"] = None

    def _process_rcm(self, msg: dict, clock_time: Optional[datetime] = None) -> None:
        """Parse a race-control message and update per-driver flag state."""
        category = msg.get("Category", "")
        text = msg.get("Message", "") or ""
        flag = msg.get("Flag", "")
        upper = text.upper()

        # ── Driver-targeted flag categories ────────────────────────────
        if category == "Flag":
            cars = self._extract_cars(text)
            primary = cars[0] if cars else msg.get("RacingNumber")
            if primary is None:
                return
            primary = str(primary)
            d = self._drivers.get(primary)
            if not d:
                return
            if flag == "BLACK":
                d["blackFlag"] = True
                self._clear_pen(d, also_track_limits=True)
                return
            if flag == "BLACK AND WHITE" and "TRACK LIMITS" in upper:
                d["trackLimitsFlag"] = True
                d["trackLimitsWarning"] = True
                return
            if flag == "BLUE" or "WAVED BLUE FLAG" in upper:
                # 10 s of session time. Session clock-ms from offset.
                offset_ms = None
                if clock_time is not None and self._start_time is not None:
                    offset_ms = int((clock_time - self._start_time).total_seconds() * 1000)
                if offset_ms is not None:
                    d["blueFlagUntilMs"] = offset_ms + 10_000
                return
            return

        # ── FIA STEWARDS messages ──────────────────────────────────────
        if "FIA STEWARDS" not in upper:
            return

        cars = self._extract_cars(text)
        reason = self._extract_reason(text)
        incident_key = (tuple(sorted(cars)), reason) if cars else None

        def for_each(action):
            for n in cars:
                d = self._drivers.get(n)
                if d:
                    action(d)

        # Resolutions (= clear the driver's pen badge).
        if "NO FURTHER ACTION" in upper or "NO FURTHER INVESTIGATION" in upper:
            for_each(self._clear_pen)
            return
        if "PENALTY SERVED" in upper:
            for_each(self._clear_pen)
            return

        # States that keep the badge on the driver(s).
        if "UNDER INVESTIGATION" in upper:
            for_each(lambda d: self._set_pen(d, "investigation", reason, incident_key))
            return
        if "WILL BE INVESTIGATED AFTER" in upper:
            for_each(lambda d: self._set_pen(d, "deferred", reason, incident_key))
            return
        # NOTED comes BEFORE the explicit penalties so a "NOTED - SAFETY
        # CAR INFRINGEMENT" line keeps its "noted" kind.
        if " NOTED" in upper or upper.endswith("NOTED"):
            for_each(lambda d: self._set_pen(d, "noted", reason, incident_key))
            return

        # Specific penalties. Multi-driver exoneration: if THIS penalty
        # has only some of the cars from a prior incident, the cars NOT
        # named here get cleared (= they were exonerated).
        pen_kind = None
        if m := self._TIME_PEN_RX.search(upper):
            pen_kind = f"{m.group(1)}s"
        elif "DRIVE THROUGH" in upper:
            pen_kind = "dt"
        elif "STOP-AND-GO" in upper or "STOP AND GO" in upper:
            pen_kind = "sg"
        if pen_kind is None:
            return
        for_each(lambda d: self._set_pen(d, pen_kind, reason, incident_key))
        # Clear track-limits flag for any car that just received a
        # track-limits-related penalty.
        if reason and "TRACK LIMITS" in reason.upper():
            for n in cars:
                d = self._drivers.get(n)
                if d:
                    d["trackLimitsFlag"] = False
                    d["trackLimitsWarning"] = False
        # Multi-driver exoneration: any driver previously named in an
        # incident with the SAME reason whose pen state still says
        # "investigation" or "deferred", and who is NOT in `cars`, gets
        # cleared.
        if reason and cars:
            named = set(cars)
            for num, d in self._drivers.items():
                if num in named: continue
                incident = d.get("penIncident")
                if not incident: continue
                prev_cars, prev_reason = incident
                if prev_reason == reason and num in set(prev_cars):
                    if d.get("penKind") in ("investigation", "deferred"):
                        self._clear_pen(d)

    # ── Highlight Expiry ──

    def _expire_highlights(self, clock_time: datetime) -> None:
        """Clear expired highlights (qualifying only)."""
        if not self._is_qualifying:
            return
        for d in self._drivers.values():
            if (d["highlight"]
                    and d["highlightStart"]
                    and (clock_time - d["highlightStart"]).total_seconds()
                    > HIGHLIGHT_DURATION_S):
                d["highlight"] = None
                d["highlightStart"] = None

    # ── Race Helpers ──

    @staticmethod
    def _get_current_stint(driver: dict) -> Optional[dict]:
        """Get current active stint (last with a real compound)."""
        for stint in reversed(driver["stints"]):
            if (stint["compound"]
                    and stint["compound"] != "UNKNOWN"
                    and not stint["tyresNotChanged"]):
                return stint
        return None

    @staticmethod
    def _get_used_compounds(driver: dict) -> tuple[set[str], set[str]]:
        """Return (dry_compounds, wet_compounds) used across all stints."""
        dry = set()
        wet = set()
        for stint in driver["stints"]:
            c = stint.get("compound")
            if not c or c == "UNKNOWN" or stint.get("tyresNotChanged"):
                continue
            if _is_wet_compound(c):
                wet.add(c)
            else:
                dry.add(c)
        return dry, wet

    def _has_tyre_requirement(self, driver: dict) -> bool:
        """Check if driver has fulfilled mandatory tyre requirement."""
        if self._is_sprint:
            return True
        dry, wet = self._get_used_compounds(driver)
        if wet:
            return True
        return len(dry) > 1

    @staticmethod
    def _get_penalty_display(
        driver: dict,
    ) -> tuple[Optional[str], Optional[str]]:
        """Return (label, css_class) for the legacy single-indicator
        badge — kept only for backwards compatibility with frontend code
        that still reads `entry.penaltyText` / `entry.penaltyClass`.

        For race + sprint sessions, the canonical penalty/flag stack now
        lives in the `fiaStewards` topic (= FiaStewardsProcessor). This
        function will be removed once the standings tile fully migrates.
        """
        if driver.get("stopped") or driver.get("retired"):
            return None, None
        if driver.get("blackFlag"):
            return "DSQ", "dsq"
        if driver.get("penalty"):
            ptype = driver["penalty"].get("type") or ""
            label_map = {"5s": "+5s", "10s": "+10s", "dt": "D-T", "sg": "S&G"}
            return label_map.get(ptype, "!"), "penalty-red"
        if driver.get("underInvestigation"):
            return "!", "warning-yellow"
        if driver.get("trackLimitsWarning"):
            return "!", "warning-white"
        return None, None

    def _get_pit_indicator(self, driver: dict) -> str:
        """Return pit indicator state."""
        if driver["inPit"]:
            return "in-pit"
        if self._has_tyre_requirement(driver):
            return "fulfilled"
        return "none"

    # ── Emission ──

    def _emit_standings(self, clock_time: datetime) -> None:
        """Emit display:standings with full driver data."""
        sorted_drivers = sorted(
            [d for d in self._drivers.values() if d["position"] < 99],
            key=lambda d: d["position"],
        )

        drivers_out = []
        p1 = sorted_drivers[0] if sorted_drivers else None

        # Derive the session-overall best lap from current bestLap values
        # — F1's `OverallFastest` flag gets lost in segment transitions
        # (we clear `bestLapOverall` for advancing drivers at the start
        # of each new quali segment, and F1 doesn't re-flag the next
        # session-best lap). Computing on emit guarantees exactly ONE
        # driver carries the purple session-best flag — for race too,
        # so the best-lap column highlights the fastest race-lap.
        overall_best_num: Optional[str] = None
        overall_best_ms: Optional[int] = None
        for d in sorted_drivers:
            ms = _parse_lap_time_ms(d["bestLap"]) if d["bestLap"] else None
            if ms is None:
                continue
            if overall_best_ms is None or ms < overall_best_ms:
                overall_best_ms = ms
                overall_best_num = d["num"]

        for d in sorted_drivers:
            entry: dict[str, Any] = {
                "num": d["num"],
                "tla": d["tla"],
                "team": d["team"],
                "color": d["color"],
                "position": d["position"],
            }

            if self._is_race:
                self._build_race_entry(entry, d, p1)
                entry["bestLapOverall"] = (d["num"] == overall_best_num)
            else:
                self._build_timing_entry(entry, d, p1, sorted_drivers)
                entry["bestLapOverall"] = (d["num"] == overall_best_num)

            drivers_out.append(entry)

        payload: dict[str, Any] = {"drivers": drivers_out}

        if self._is_race:
            payload["currentLap"] = self._current_lap
            payload["totalLaps"] = self._total_laps
            payload["isSprint"] = self._is_sprint

        if self._is_qualifying:
            payload["qualifyingSegment"] = self._qualifying_segment

        self._bus.emit("display:standings", payload, clock_time)

    def _build_timing_entry(
        self,
        entry: dict,
        d: dict,
        p1: Optional[dict],
        sorted_drivers: list[dict],
    ) -> None:
        """Build practice/qualifying driver entry."""
        entry["finished"] = d["finished"]
        entry["bestLap"] = d["bestLap"]
        entry["bestLapPersonal"] = d["bestLapPersonal"]
        entry["bestLapOverall"] = d["bestLapOverall"]
        entry["bestLapTyreCompound"] = d["bestLapTyreCompound"]
        entry["bestLapTyreIsNew"] = d["bestLapTyreIsNew"]

        # Gap computation
        gap_text = ""
        gap_is_red = False

        if d["num"] in self._eliminated:
            entry["knockedOut"] = True
        elif d["gap"]:
            gap_text = d["gap"]
        elif d["position"] > 1 and p1 and p1["bestLap"] and d["bestLap"]:
            p1_ms = _parse_lap_time_ms(p1["bestLap"])
            d_ms = _parse_lap_time_ms(d["bestLap"])
            if p1_ms is not None and d_ms is not None:
                if self._is_qualifying:
                    gap_text, gap_is_red = self._compute_qualifying_gap(
                        d, d_ms, p1_ms, sorted_drivers
                    )
                else:
                    gap_text = _format_gap(d_ms - p1_ms)

        entry["gap"] = gap_text
        entry["gapIsRed"] = gap_is_red

        if self._is_qualifying:
            entry["highlight"] = d["highlight"]
            entry["knockedOut"] = d["num"] in self._eliminated

    def _compute_qualifying_gap(
        self,
        d: dict,
        d_ms: float,
        p1_ms: float,
        sorted_drivers: list[dict],
    ) -> tuple[str, bool]:
        """Compute qualifying gap with knockout zone awareness."""
        driver_count = len(sorted_drivers) + len(self._eliminated)
        q1_knockout = 16 if driver_count > 20 else 15
        q2_knockout = 10
        seg = self._qualifying_segment or ""

        if seg in ("Q1", "SQ1"):
            if d["position"] <= q1_knockout:
                return _format_gap(d_ms - p1_ms), False
            else:
                cutoff = (
                    sorted_drivers[q1_knockout - 1]
                    if len(sorted_drivers) >= q1_knockout
                    else None
                )
                if cutoff and cutoff["bestLap"]:
                    cut_ms = _parse_lap_time_ms(cutoff["bestLap"])
                    if cut_ms is not None:
                        return _format_gap(d_ms - cut_ms), True
        elif seg in ("Q2", "SQ2"):
            if d["position"] <= q2_knockout:
                return _format_gap(d_ms - p1_ms), False
            else:
                cutoff = (
                    sorted_drivers[q2_knockout - 1]
                    if len(sorted_drivers) >= q2_knockout
                    else None
                )
                if cutoff and cutoff["bestLap"]:
                    cut_ms = _parse_lap_time_ms(cutoff["bestLap"])
                    if cut_ms is not None:
                        return _format_gap(d_ms - cut_ms), True
        else:
            return _format_gap(d_ms - p1_ms), False

        return _format_gap(d_ms - p1_ms), False

    def _build_race_entry(
        self, entry: dict, d: dict, p1: Optional[dict]
    ) -> None:
        """Build race driver entry."""
        # Gap: P1 shows "LAP X/Y"; retired/stopped/DSQ show the lap they
        # went out on (= e.g. "L18") instead of generic STOP/DSQ text.
        if d.get("retired") or d.get("stopped") or d["blackFlag"]:
            lap_out = d.get("retiredOnLap") or d.get("lastLapNumber") or 0
            entry["gap"] = f"L{lap_out}" if lap_out else "—"
        elif d is p1:
            if self._current_lap and self._total_laps:
                entry["gap"] = f"LAP {self._current_lap}/{self._total_laps}"
            else:
                entry["gap"] = ""
        else:
            entry["gap"] = d.get("gap") or ""

        entry["interval"] = d["interval"]
        entry["inPit"] = d["inPit"]
        entry["retired"] = d["retired"]
        entry["stopped"] = d["stopped"]
        entry["blackFlag"] = d["blackFlag"]
        entry["finished"] = d["finished"]

        # Best lap per driver — race needs these too so the standings
        # tile can populate the Best lap column with every driver's PB.
        entry["bestLap"] = d["bestLap"]
        entry["bestLapPersonal"] = d["bestLapPersonal"]
        # (bestLapOverall is overridden in _emit_standings from the
        # derived session-overall calculation.)

        # Penalty display
        pen_text, pen_class = self._get_penalty_display(d)
        entry["penaltyText"] = pen_text
        entry["penaltyClass"] = pen_class

        # Pit indicator
        entry["pitIndicator"] = self._get_pit_indicator(d)

        # Current stint
        current_stint = self._get_current_stint(d)
        if current_stint:
            entry["currentStint"] = {
                "compound": current_stint["compound"],
                "isNew": current_stint["isNew"],
                "laps": current_stint["localLapCount"] or current_stint["totalLaps"],
            }
        else:
            entry["currentStint"] = None

    # ── Snapshot / Restore ──

    def snapshot(self) -> dict[str, Any]:
        import copy
        drivers_snap = {}
        for num, d in self._drivers.items():
            snap = dict(d)
            # Convert datetime to ISO for serialization
            if snap["highlightStart"]:
                snap["highlightStart"] = snap["highlightStart"].isoformat()
            # Deep copy stints
            snap["stints"] = [dict(s) for s in snap["stints"]]
            # Clear highlights (they're transient)
            snap["highlight"] = None
            snap["highlightStart"] = None
            drivers_snap[num] = snap

        return {
            "drivers": drivers_snap,
            "qualifying_segment": self._qualifying_segment,
            "eliminated": list(self._eliminated),
            "is_sprint_quali": self._is_sprint_quali,
            "current_lap": self._current_lap,
            "total_laps": self._total_laps,
            "is_sprint": self._is_sprint,
            "last_rcm_key": self._last_rcm_key,
            "chequered_seen": self._chequered_seen,
            "lap_at_chequered": dict(self._lap_at_chequered),
            "first_flag_driver": self._first_flag_driver,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self._drivers = state.get("drivers", {})
        # Restore stints as proper lists of dicts
        for d in self._drivers.values():
            d["stints"] = [dict(s) for s in d.get("stints", [])]
            d["highlightStart"] = None
            d["highlight"] = None

        self._qualifying_segment = state.get("qualifying_segment")
        self._eliminated = set(state.get("eliminated", []))
        self._is_sprint_quali = state.get("is_sprint_quali", False)
        self._current_lap = state.get("current_lap")
        self._total_laps = state.get("total_laps")
        self._is_sprint = state.get("is_sprint", False)
        self._last_rcm_key = state.get("last_rcm_key", -1)
        self._chequered_seen = state.get("chequered_seen", False)
        self._lap_at_chequered = dict(state.get("lap_at_chequered", {}))
        self._first_flag_driver = state.get("first_flag_driver")

    def reset(self) -> None:
        for d in self._drivers.values():
            d["position"] = 99
            d["bestLap"] = None
            d["bestLapPersonal"] = False
            d["bestLapOverall"] = False
            d["bestLapTyreCompound"] = None
            d["bestLapTyreIsNew"] = None
            d["currentTyreCompound"] = None
            d["currentTyreIsNew"] = None
            d["gap"] = None
            d["highlight"] = None
            d["highlightStart"] = None
            d["interval"] = None
            d["inPit"] = False
            d["retired"] = False
            d["stopped"] = False
            d["stints"] = []
            d["lastLapNumber"] = 0
            d["underInvestigation"] = False
            d["penalty"] = None
            d["trackLimitsWarning"] = False
            d["blackFlag"] = False
            d["finished"] = False

        self._qualifying_segment = None
        self._eliminated = set()
        self._current_lap = None
        self._is_sprint = False
        self._last_rcm_key = -1
        self._chequered_seen = False
        self._lap_at_chequered = {}
        self._first_flag_driver = None
        # Keep: total_laps, is_sprint_quali, driver identity (tla, team, color)
