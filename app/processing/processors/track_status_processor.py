"""
Track Status Processor — F1-broadcast-correct track status state machine.

Subscribes to: RaceControlMessages, TrackStatus
Emits: trackStatus, event

Inputs:
  - RaceControlMessages (text field, plus Category=Flag)
  - TrackStatus topic (numeric Status code 1-7)

States: TRACK_CLEAR, GREEN, YELLOW, SC, VSC, RED, CHEQUERED.

Rules (per F1 broadcast conventions):
  1. Initial: a "TRACK CLEAR" RCM at session start → state TRACK_CLEAR
     ("Track Clear" shown in white-on-transparent in the header).
     Flag messages do not change this state until session officially goes
     green.
  2. "GREEN LIGHT - PIT EXIT OPEN" RCM → GREEN, with race-specific count:
     - practice/qualifying: every such message → GREEN.
     - race: 1st such message (cars to grid) does NOT change state; the
       2nd (race start / restart) → GREEN. Counter resets after RED.
  3. Plain yellow flag (RCM Flag=YELLOW, or TrackStatus=2) → no change.
  4. SC/VSC (TrackStatus 4/6) → SC / VSC (visually yellow).
  5. Under SC/VSC, "TRACK CLEAR" RCM → GREEN (SC/VSC end).
  6. Red flag (TrackStatus 5 or RCM Flag=RED) → RED. Only "GREEN LIGHT -
     PIT EXIT OPEN" clears RED (2nd in race, 1st in practice/quali).

Playback-scrubber events (separate from trackStatus display):
  - RED, CHEQUERED, SC, VSC: always scrubber events.
  - GREEN: scrubber event only when clearing SC / VSC / RED. (In
    practice/quali, GREEN out of TRACK_CLEAR is also a scrubber event —
    it marks the session going green.)
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


_GREEN_LIGHT_RE = "GREEN LIGHT"
_PIT_EXIT_OPEN_RE = "PIT EXIT OPEN"
_TRACK_CLEAR_RE = "TRACK CLEAR"


class TrackStatusProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._last_status: Optional[str] = None
        self._last_rcm_key: int = -1
        # Race start / restart counter: counts "GREEN LIGHT - PIT EXIT
        # OPEN" RCMs since session start (or since last RED). In race,
        # the 2nd one marks the actual race start / restart; in
        # practice / qualifying every one means GREEN.
        self._green_light_count: int = 0

    # F1 TrackStatus numeric codes
    _STATUS_MAP = {
        "1": "GREEN", "2": "YELLOW", "4": "SC", "5": "RED",
        "6": "VSC", "7": "CHEQUERED",
    }

    def subscribe(self) -> None:
        self._bus.on("RaceControlMessages", self._handle_rcm)
        self._bus.on("TrackStatus", self._handle_track_status)
        # Race GREEN is driven by SessionStatus=Started, NOT the
        # "GREEN LIGHT - PIT EXIT OPEN" RCM. The green-light RCM is
        # unreliable for restarts (a red-flag → standing-start restart
        # sends no green light at all — Monaco 2026), whereas
        # SessionStatus=Started fires at both the original start and every
        # restart. After GREEN, the SC/VSC + AllClear TrackStatus codes
        # drive the rest (SCDeployed → SC, AllClear → GREEN). Emitted by
        # SessionDataProcessor.
        if self._is_race:
            self._bus.on("sessionStatus", self._handle_session_status)
        # If the data capture started after the "GREEN LIGHT - PIT EXIT
        # OPEN" RCM was broadcast (rare but real — Miami FP1 2026),
        # the green-light trigger is missing and we'd be stuck at
        # TRACK_CLEAR forever. Cars on track ⇒ green-light was issued
        # before capture (F1 protocol requires it) ⇒ auto-emit GREEN
        # on first sign of car movement.
        self._bus.on("TimingData", self._handle_timing_data_for_implicit_green)

    def _handle_timing_data_for_implicit_green(self, data: Any, clock_time: datetime) -> None:
        """If TimingData shows cars on track while we're still in
        TRACK_CLEAR, the green-light RCM must have fired before capture
        started — emit GREEN now so the rest of the pipeline (events,
        scrubber, race-finish detection) behaves correctly.

        "Cars on track" = at least one driver with `InPit=False`, OR
        any driver that has completed at least one lap.

        Race-start guard: in RACE mode the 1st "GREEN LIGHT - PIT EXIT
        OPEN" RCM means cars-to-grid (no state change); we MUST wait
        for the 2nd one. If we've already received ANY green-light RCM,
        the real green will come via the RCM path — firing implicit-
        GREEN now would steal the slot and the proper race-start emit
        would be suppressed by _emit's dedup gate.
        """
        if self._last_status != "TRACK_CLEAR":
            return
        if self._green_light_count > 0:
            return
        if not isinstance(data, dict):
            return
        lines = data.get("Lines") or data
        if not isinstance(lines, dict):
            return
        for timing in lines.values():
            if not isinstance(timing, dict):
                continue
            if timing.get("InPit") is False:
                self._emit("GREEN", clock_time)
                return
            try:
                if int(timing.get("NumberOfLaps") or 0) >= 1:
                    self._emit("GREEN", clock_time)
                    return
            except (TypeError, ValueError):
                continue

    # ─────────────────────────────── TrackStatus ──────────────────────

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        """Raw F1 TrackStatus: {"Status": "1", "Message": "AllClear"}.

        Note: F1 overloads Status=7 — it can mean either the chequered
        flag OR "VSC ending" (depending on `Message`). We disambiguate
        by the message text.
        """
        if not isinstance(data, dict):
            return
        code = str(data.get("Status", ""))
        msg = (data.get("Message") or "").lower()
        status = self._STATUS_MAP.get(code)
        if status is None:
            return

        # Disambiguate Status=7: "vscending" / "scending" → SC/VSC ended,
        # treat as a transition back to GREEN. Only "chequered" really
        # means chequered.
        if code == "7" and "ending" in msg:
            if self._last_status in ("SC", "VSC"):
                self._emit("GREEN", clock_time)
            return

        # Rule 3: bare yellow flag never changes display.
        if status == "YELLOW":
            return

        # RED and CHEQUERED: always emit (RED resets the green-light
        # counter — race restart needs 2 fresh pit-exit messages).
        if status == "RED":
            self._green_light_count = 0
            self._emit(status, clock_time)
            return
        if status == "CHEQUERED":
            self._emit(status, clock_time)
            return

        # SC / VSC: only flag when the session was actually green (or
        # already under SC/VSC — VSC → SC upgrade etc.). Pre-session
        # (TRACK_CLEAR) and red-flag (RED) periods get ignored —
        # safety-car deployment during those phases is operational
        # noise, not a meaningful track-status change.
        if status in ("SC", "VSC"):
            if self._last_status in ("GREEN", "SC", "VSC"):
                self._emit(status, clock_time)
            return

        # Status 1 (GREEN / AllClear):
        #  - At session start (no prior state) → emit TRACK_CLEAR
        #    (the pre-session "Track Clear" indicator — rule 1).
        #  - Coming out of SC / VSC → emit GREEN.
        #  - From TRACK_CLEAR / RED / GREEN → ignore (GREEN is gated by
        #    the "GREEN LIGHT - PIT EXIT OPEN" RCM per rules 1 + 6).
        if status == "GREEN":
            if self._last_status is None:
                self._emit("TRACK_CLEAR", clock_time)
            elif self._last_status in ("SC", "VSC"):
                self._emit("GREEN", clock_time)

    # ───────────────────────── SessionStatus ──────────────────────────

    def _handle_session_status(self, data: Any, clock_time: datetime) -> None:
        """Race GREEN trigger (= SME 2026-06-07): SessionStatus=Started
        means the race is running. At the original start it coincides with
        lights-out; at a restart it fires where no green-light RCM exists.
        Emit GREEN — the SC/VSC + AllClear TrackStatus codes then drive
        the rest. `_emit` dedupes, so a co-incident green-light RCM is a
        no-op. (Aborted → RED is already covered by TrackStatus=5 / the
        RED FLAG RCM, so we don't duplicate it here.)
        """
        if data == "Started":
            self._emit("GREEN", clock_time)

    # ───────────────────────── RaceControlMessages ────────────────────

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        messages = data.get("Messages")
        if not isinstance(messages, (dict, list)):
            return

        if isinstance(messages, dict):
            items = sorted(messages.items(),
                           key=lambda x: int(x[0]) if x[0].isdigit() else 0)
            for key_str, msg in items:
                if not isinstance(msg, dict):
                    continue
                try:
                    idx = int(key_str)
                except (ValueError, TypeError):
                    continue
                if idx <= self._last_rcm_key:
                    continue
                self._last_rcm_key = idx
                self._process_rcm(msg, clock_time)
        else:
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if i <= self._last_rcm_key:
                    continue
                self._last_rcm_key = i
                self._process_rcm(msg, clock_time)

    def _process_rcm(self, msg: dict, clock_time: datetime) -> None:
        category = msg.get("Category", "")
        flag = (msg.get("Flag") or "").upper()
        scope = (msg.get("Scope") or "").upper()
        text = (msg.get("Message") or "").upper()

        # "GREEN LIGHT - PIT EXIT OPEN" — session start / restart trigger.
        if _GREEN_LIGHT_RE in text and _PIT_EXIT_OPEN_RE in text:
            self._green_light_count += 1
            if self._is_race:
                # 1st = cars going to grid (no state change); 2nd = race
                # start / restart.
                if self._green_light_count >= 2:
                    self._emit("GREEN", clock_time)
            else:
                # Practice / qualifying: every such message → GREEN.
                self._emit("GREEN", clock_time)
            return

        # "TRACK CLEAR" RCM — dual-purpose depending on current state.
        if _TRACK_CLEAR_RE in text:
            if self._last_status is None:
                # Initial: enter the pre-session TRACK_CLEAR state.
                self._emit("TRACK_CLEAR", clock_time)
            elif self._last_status in ("SC", "VSC"):
                # SC / VSC ended (rule 5).
                self._emit("GREEN", clock_time)
            elif self._last_status == "RED":
                # Red flag lifted: track is cleared but the race hasn't
                # formally restarted yet. Drop back to TRACK_CLEAR as the
                # intermediate "track safe, awaiting restart" badge. The
                # full RED → GREEN transition still requires the green-
                # light RCM (= rule 6, restart). Without this, the badge
                # stayed on RED indefinitely after a red flag was lifted
                # (= Monaco 2026 race observation).
                self._emit("TRACK_CLEAR", clock_time)
            # Under GREEN / TRACK_CLEAR / CHEQUERED: no change.
            return

        # Flag messages.
        if category == "Flag" and scope == "TRACK":
            if flag == "RED":
                self._green_light_count = 0
                self._emit("RED", clock_time)
            elif flag == "CHEQUERED":
                self._emit("CHEQUERED", clock_time)
            elif flag == "GREEN":
                self._emit("GREEN", clock_time)
            # YELLOW flag → ignored (rule 3); CLEAR → handled via the
            # "TRACK CLEAR" text branch above.
            return

    # ─────────────────────────────── Emit ─────────────────────────────

    def _emit(self, status: str, clock_time: datetime) -> None:
        # Sticky RED guard: SC / VSC / YELLOW can't override RED.
        if self._last_status == "RED" and status in ("YELLOW", "SC", "VSC"):
            return
        if status == self._last_status:
            return

        prev = self._last_status
        self._last_status = status
        self._bus.emit("trackStatus", status, clock_time)

        # Scrubber event: RED / CHEQUERED / SC / VSC always; GREEN only
        # when clearing a real restriction.
        emit_event = True
        if status == "GREEN":
            if self._is_race:
                # Race start (TRACK_CLEAR → GREEN on 2nd green-light)
                # AND restarts (SC/VSC/RED → GREEN) are scrubber events.
                emit_event = prev in ("TRACK_CLEAR", "SC", "VSC", "RED")
            else:
                # Practice / qualifying: session-go-green AND quali
                # segment starts (CHEQUERED → GREEN at Q2 / Q3) plus
                # restarts from SC / VSC / RED. Suppressing CHEQUERED
                # here also caused the dedup in _capture_output to drop
                # the NEXT CHEQUERED — see track-status-scrubber-events-fix.
                emit_event = prev in (
                    "TRACK_CLEAR", "CHEQUERED", "SC", "VSC", "RED",
                )
        elif status in ("SC", "VSC"):
            # Only emit a scrubber event when SC/VSC was deployed FROM
            # green — i.e. an actual on-track incident during racing.
            # SC → VSC and VSC → SC transitions within the same incident
            # don't add a second event (same root cause).
            emit_event = prev == "GREEN"
        elif status == "TRACK_CLEAR":
            # Pre-session indicator — session start is emitted elsewhere.
            emit_event = False
        if emit_event:
            self._bus.emit("event", status, clock_time)

    # ─────────────────────────── Snapshot ─────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_status": self._last_status,
            "last_rcm_key": self._last_rcm_key,
            "green_light_count": self._green_light_count,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self._last_status = state.get("last_status")
        self._last_rcm_key = state.get("last_rcm_key", -1)
        self._green_light_count = state.get("green_light_count", 0)

    def reset(self) -> None:
        self._last_status = None
        self._last_rcm_key = -1
        self._green_light_count = 0
