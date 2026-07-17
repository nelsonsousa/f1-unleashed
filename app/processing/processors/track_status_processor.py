"""
Track Status Processor — single combined status badge.

Subscribes to: SessionStatus, TrackStatus, RaceControlMessages
Emits:
  trackStatus  {status, message} — the badge state
  event        scrubber marker token (GREEN/RED/SC/VSC/CHEQUERED), once per
               colour change

The badge combines SessionStatus (authoritative session state) with the
safety-car states from TrackStatus and the "safety car in this lap" RCM:

  SessionStatus.Status:
    Inactive  -> inactive  "--"
    Started   -> green     "GREEN FLAG"
    Aborted   -> red       "RED FLAG"
    Finished  -> finished  "CHECKERED FLAG"
    Finalised -> finished  "SESSION FINISHED"
    Ends      -> finished  "SESSION ENDED"

  TrackStatus.Message (safety-car overlay — applies only while RACING):
    SCDeployed  -> sc    "SC DEPLOYED"
    VSCDeployed -> vsc   "VSC DEPLOYED"
    VSCEnding   -> vsc   "VSC ENDING"
    AllClear    -> green "GREEN FLAG"   (lifts an sc/vsc caution)

  RaceControlMessages (Category=SafetyCar, Mode="SAFETY CAR"):
    Status=DEPLOYED     -> sc "SC DEPLOYED"
    Status=IN THIS LAP  -> sc "SC IN THIS LAP"

The safety-car states overlay a RACING session (status in green/sc/vsc) and may
follow one another in ANY order — there is no fixed progression, so the SC can go
IN THIS LAP -> DEPLOYED again when it's extended (FI4QUOaJ). They do NOT override
the authoritative SessionStatus red/finished/inactive, and AllClear only lifts an
sc/vsc caution — so a RED (SessionStatus=Aborted) is cleared only by the next
SessionStatus=Started.

Client colours: green->green, red->red, sc/vsc->yellow, inactive/finished->
clear. The scrubber marker fires only when that colour changes, so consecutive
yellows (SC<->VSC, VSC ENDING, SC IN THIS LAP/DEPLOYED) add no extra marker.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

_SESSION_STATUS_MAP = {
    "Inactive":  ("inactive", "--"),
    "Started":   ("green",    "GREEN FLAG"),
    "Aborted":   ("red",      "RED FLAG"),
    "Finished":  ("finished", "CHECKERED FLAG"),
    "Finalised": ("finished", "SESSION FINISHED"),
    "Ends":      ("finished", "SESSION ENDED"),
}

# Scrubber colour per status (the marker fires once per colour change).
_STATUS_COLOUR = {
    "green": "green", "red": "red", "sc": "yellow", "vsc": "yellow",
    "finished": "chequered", "inactive": "clear",
}
# Scrubber marker token per status (None = no marker for that colour).
_STATUS_EVENT = {
    "green": "GREEN", "red": "RED", "sc": "SC", "vsc": "VSC",
    "finished": "CHEQUERED",
}


class TrackStatusProcessor(Processor):
    """Combines SessionStatus + TrackStatus into a single badge status."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._status: str = ""
        self._message: str = ""
        self._event_colour: Optional[str] = None

    def subscribe(self) -> None:
        self._bus.on("SessionStatus", self._handle_session_status)
        self._bus.on("TrackStatus", self._handle_track_status)
        self._bus.on("RaceControlMessages", self._handle_rcm)

    def _handle_session_status(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        entry = _SESSION_STATUS_MAP.get(data.get("Status"))
        if entry:
            self._set(entry[0], entry[1], clock_time)

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        # Safety-car overlay applies only while racing (green/sc/vsc); within that
        # there's no fixed progression — any caution can follow any other.
        msg = data.get("Message")
        racing = self._status in ("green", "sc", "vsc")
        if msg == "SCDeployed" and racing:
            self._set("sc", "SC DEPLOYED", clock_time)
        elif msg == "VSCDeployed" and racing:
            self._set("vsc", "VSC DEPLOYED", clock_time)
        elif msg == "VSCEnding" and racing:
            self._set("vsc", "VSC ENDING", clock_time)
        elif msg == "AllClear" and self._status in ("sc", "vsc"):
            self._set("green", "GREEN FLAG", clock_time)

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        messages = data.get("Messages")
        if isinstance(messages, dict):
            items = list(messages.values())
        elif isinstance(messages, list):
            items = messages
        else:
            return
        # SC state from RCM (Category=SafetyCar, Mode="SAFETY CAR"): DEPLOYED and
        # IN THIS LAP set their state directly, in EITHER order — F1 does NOT
        # re-send TrackStatus when the SC is extended (code stays 4), so the
        # "IN THIS LAP → DEPLOYED again" re-arm only arrives here (FI4QUOaJ). VSC
        # (Mode="VSC") is driven by TrackStatus, so it's ignored here; overlays
        # racing only, and dedup makes a repeat message a no-op.
        for m in items:
            if not isinstance(m, dict) or m.get("Category") != "SafetyCar":
                continue
            if m.get("Mode") != "SAFETY CAR" or self._status not in ("green", "sc", "vsc"):
                continue
            status = m.get("Status")
            if status == "DEPLOYED":
                self._set("sc", "SC DEPLOYED", clock_time)
            elif status == "IN THIS LAP":
                self._set("sc", "SC IN THIS LAP", clock_time)

    def _set(self, status: str, message: str, clock_time: datetime) -> None:
        if status == self._status and message == self._message:
            return
        self._status = status
        self._message = message
        self._bus.emit("trackStatus", {"status": status, "message": message}, clock_time)

        # Scrubber marker: once per colour change (so consecutive yellows —
        # SC->VSC, VSC ENDING, SC IN THIS LAP — add no extra marker).
        colour = _STATUS_COLOUR.get(status, "clear")
        if colour != self._event_colour:
            self._event_colour = colour
            token = _STATUS_EVENT.get(status)
            if token:
                self._bus.emit("event", token, clock_time)
