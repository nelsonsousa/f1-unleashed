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

  TrackStatus.Message (guarded — see below):
    SCDeployed  -> sc   "SC DEPLOYED"   (only if status in green/sc/vsc)
    VSCDeployed -> vsc  "VSC DEPLOYED"  (only if status in green/sc/vsc)
    VSCEnding   -> vsc  "VSC ENDING"    (only if status == vsc)
    AllClear    -> green "GREEN FLAG"   (only if status in sc/vsc; else ignored)

  RaceControlMessages (SafetyCar / "IN THIS LAP"):
    -> sc "SC IN THIS LAP"  (only if status == sc)

Guards mean a RED (SessionStatus=Aborted) is never cleared by a TrackStatus
AllClear — only the next SessionStatus=Started restores green.

Client colours: green->green, red->red, sc/vsc->yellow, inactive/finished->
clear. The scrubber marker fires only when that colour changes, so the
yellow->yellow transitions (SC->VSC, VSC ENDING, SC IN THIS LAP) add no marker.
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
        msg = data.get("Message")
        if msg == "SCDeployed":
            if self._status in ("green", "sc", "vsc"):
                self._set("sc", "SC DEPLOYED", clock_time)
        elif msg == "VSCDeployed":
            if self._status in ("green", "sc", "vsc"):
                self._set("vsc", "VSC DEPLOYED", clock_time)
        elif msg == "VSCEnding":
            if self._status == "vsc":
                self._set("vsc", "VSC ENDING", clock_time)
        elif msg == "AllClear":
            if self._status in ("sc", "vsc"):
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
        for m in items:
            if not isinstance(m, dict):
                continue
            if m.get("Category") == "SafetyCar" and m.get("Status") == "IN THIS LAP":
                if self._status == "sc":
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
