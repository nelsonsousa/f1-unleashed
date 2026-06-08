"""
Event Detector — scrubber event markers.

Subscribes to: ExtrapolatedClock, SessionData, RaceControlMessages,
               TimingData, TimingAppData, DriverList
Emits: display:events

Ports the scan-ahead event detection from header.js (scanNewMessages,
scanRaceControlMessages, scanExtrapolatedClock, scanSessionData,
scanTimingData, scanTimingAppData).

This processor runs in the background scanner as well as during playback,
detecting session events (flags, safety cars, Q-parts, retirements, P1
changes, tyre crossovers) and emitting them as scrubber markers.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _is_wet_compound(compound: Optional[str]) -> bool:
    if not compound:
        return False
    c = compound.upper()
    return c in ("INTERMEDIATE", "WET")


class EventDetector(Processor):
    """Detects session events for scrubber markers."""

    def __init__(
        self,
        bus: SessionMessageBus,
        session_type: str,
        start_time: Optional[datetime] = None,
    ):
        super().__init__(bus, session_type)
        self._start_time = start_time
        self._is_race = session_type == "race"

        # Driver TLA lookup
        self._driver_tlas: dict[str, str] = {}

        # Event dedup
        self._events: list[dict[str, Any]] = []
        self._event_keys: set[str] = set()

        # Clock state
        self._extrapolating_count = 0
        self._was_extrapolating = False

        # Session data
        self._session_badge: Optional[str] = None
        self._is_sprint_quali = False

        # Race-specific state
        self._current_p1: Optional[str] = None
        self._tyres_published = False
        self._driver_compounds: dict[str, str] = {}
        self._stopped_drivers: set[str] = set()
        self._first_wet_switch = False
        self._first_dry_switch = False
        self._last_rcm_key = -1

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle_driver_list)
        self._bus.on("RaceControlMessages", self._handle_rcm)
        self._bus.on("ExtrapolatedClock", self._handle_extrapolated_clock)
        self._bus.on("SessionData", self._handle_session_data)
        if self._is_race:
            self._bus.on("TimingData", self._handle_timing_data)
            self._bus.on("TimingAppData", self._handle_timing_app_data)

    def _get_tla(self, driver_num: str) -> str:
        return self._driver_tlas.get(driver_num, driver_num)

    def _offset_seconds(self, clock_time: datetime) -> float:
        if not self._start_time:
            return 0.0
        return (clock_time - self._start_time).total_seconds()

    def _add_event(
        self, clock_time: datetime, event_type: str, label: str, icon: str
    ) -> bool:
        """Add an event with deduplication. Returns True if added."""
        offset = self._offset_seconds(clock_time)

        # Merge retirements within 60s
        if event_type == "stopped":
            for existing in self._events:
                if existing["type"] == "stopped" and abs(existing["offset"] - offset) < 60:
                    new_tla = label.replace(" retired", "")
                    existing["label"] = (
                        existing["label"].replace(" retired", "")
                        + ", "
                        + new_tla
                        + " retired"
                    )
                    self._emit_events(clock_time)
                    return True

        dedup_key = f"{event_type}:{round(offset / 5) * 5}"
        if dedup_key in self._event_keys:
            return False

        self._event_keys.add(dedup_key)
        self._events.append({
            "offset": offset,
            "type": event_type,
            "label": label,
            "icon": icon,
        })
        self._events.sort(key=lambda e: e["offset"])
        self._emit_events(clock_time)
        return True

    def _emit_events(self, clock_time: datetime) -> None:
        self._bus.emit("display:events", {
            "events": self._events,
        }, clock_time)

    # ── Handlers ──

    def _handle_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for driver_num, info in data.items():
            if isinstance(info, dict) and info.get("Tla"):
                self._driver_tlas[driver_num] = info["Tla"]

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
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

            # In race, skip events before tyres are known
            if self._is_race and not self._tyres_published:
                continue

            if msg.get("Category") == "Flag" and msg.get("Scope") == "Track":
                flag = msg.get("Flag", "")
                if flag in ("GREEN", "CLEAR"):
                    if not self._is_race:
                        self._add_event(clock_time, "green_flag", "GREEN", "green_flag")
                elif flag == "RED":
                    self._add_event(clock_time, "red_flag", "RED FLAG", "red_flag")
                elif flag == "CHEQUERED":
                    self._add_event(clock_time, "chequered", "Checkered flag", "chequered_flag")

            elif msg.get("Category") == "SafetyCar":
                status = msg.get("Status", "")
                if status == "THROUGH THE PIT LANE":
                    continue
                if status == "DEPLOYED":
                    self._add_event(clock_time, "sc_deployed", "SC/VSC DEPLOYED", "yellow_flag")
                elif status in ("IN THIS LAP", "ENDING"):
                    self._add_event(clock_time, "sc_end", "SC/VSC END", "green_flag")

    def _handle_extrapolated_clock(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if "Extrapolating" not in data:
            return

        was = self._was_extrapolating
        self._was_extrapolating = data["Extrapolating"]

        if data["Extrapolating"] and not was:
            self._extrapolating_count += 1

            if self._is_race:
                if not self._tyres_published:
                    return
                if self._extrapolating_count == 1:
                    self._add_event(clock_time, "race_start", "Race start", "green_flag")
                else:
                    self._add_event(clock_time, "restart", "RESTART", "green_flag")
            elif self._extrapolating_count == 1:
                self._add_event(clock_time, "session_start", "SESSION START", "green_flag")

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        # Session end detection (all session types)
        status = data.get("Status")
        if status in ("Finished", "Finalised"):
            self._add_event(clock_time, "chequered", "Session end", "chequered_flag")

        if self._session_type != "qualifying":
            return
        series = data.get("Series")
        if not series or not isinstance(series, dict):
            return

        entries = list(series.values())
        if not entries:
            return

        latest = entries[-1]
        q_part = latest.get("QualifyingPart")
        if not isinstance(q_part, int) or not (1 <= q_part <= 3):
            return

        prefix = "S" if self._is_sprint_quali else ""
        new_badge = f"{prefix}Q{q_part}"
        if new_badge != self._session_badge:
            self._session_badge = new_badge
            self._add_event(clock_time, "q_start", new_badge, "green_flag")

    def _handle_timing_data(self, data: Any, clock_time: datetime) -> None:
        """Race-only: P1 changes and retirements."""
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        for driver_num, timing in lines.items():
            if not isinstance(timing, dict):
                continue

            # P1 tracking
            pos = timing.get("Position")
            if pos in ("1", 1):
                if (self._tyres_published
                        and self._current_p1 is not None
                        and self._current_p1 != driver_num):
                    if timing.get("Tla"):
                        self._driver_tlas[driver_num] = timing["Tla"]
                    tla = self._get_tla(driver_num)
                    self._add_event(clock_time, "p1_change", f"P1: {tla}", "p1")
                self._current_p1 = driver_num

            # Stopped driver detection
            if (self._tyres_published
                    and timing.get("Stopped") is True
                    and driver_num not in self._stopped_drivers):
                self._stopped_drivers.add(driver_num)
                if timing.get("Tla"):
                    self._driver_tlas[driver_num] = timing["Tla"]
                tla = self._get_tla(driver_num)
                self._add_event(clock_time, "stopped", f"{tla} retired", "retirement")

    def _handle_timing_app_data(self, data: Any, clock_time: datetime) -> None:
        """Race-only: tyre events and crossover detection."""
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return

        for driver_num, app_data in lines.items():
            if not isinstance(app_data, dict):
                continue
            stints = app_data.get("Stints")
            if not stints or not isinstance(stints, dict):
                continue

            # Find latest stint compound
            latest_compound = None
            for stint_info in stints.values():
                if isinstance(stint_info, dict) and stint_info.get("Compound"):
                    latest_compound = stint_info["Compound"]
            if not latest_compound:
                continue

            prev_compound = self._driver_compounds.get(driver_num)
            self._driver_compounds[driver_num] = latest_compound

            # First tyre data published
            if not self._tyres_published:
                self._tyres_published = True
                any_wet = any(
                    _is_wet_compound(c) for c in self._driver_compounds.values()
                )
                icon = "tyre_wet" if any_wet else "tyre_dry"
                self._add_event(clock_time, "tyres", "Grid formation", icon)

            # Crossover detection
            if prev_compound and prev_compound != latest_compound:
                now_wet = _is_wet_compound(latest_compound)

                if now_wet and not self._first_wet_switch:
                    if not _is_wet_compound(prev_compound):
                        self._first_wet_switch = True
                        self._add_event(
                            clock_time, "crossover_wet", "DRY\u2192WET", "tyre_wet"
                        )

                if not now_wet and not self._first_dry_switch:
                    if _is_wet_compound(prev_compound):
                        self._first_dry_switch = True
                        self._add_event(
                            clock_time, "crossover_dry", "WET\u2192DRY", "tyre_dry"
                        )
