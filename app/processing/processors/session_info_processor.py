"""
Session Info Processor — session identity metadata + display badge.

Subscribes to: SessionInfo, qualifyingPart
Emits (each self-contained — restore = latest row per topic):
  meetingName    Event/meeting name string (e.g. "Monaco Grand Prix")
  sessionBadge   Display badge string (FP1 / Q / Q1 / SQ2 / S / R …)
  trackCircuit   Circuit name normalised for the SVG basename
                 (accents dropped, spaces -> underscores; "Monte_Carlo")
  sessionInfo    Raw session metadata: sessionType, sessionName,
                 sessionNumber, qualifyingPart, gmtOffset, sessionStatus

The badge mapping (formerly in header.js) is computed here:
  Practice    -> "FP" + session Number          (FP1, FP2, FP3)
  Qualifying  -> initials(Name) [+ qualifyingPart if >= 1]
                 ("Qualifying" -> Q/Q1/Q2/Q3; "Sprint Qualifying" -> SQ/SQ1…)
  Race        -> initials(Name)                  ("Race" -> R; "Sprint" -> S)

Filters out SessionInfo messages from a different session (can happen when
the previous session's archive hasn't completed before the new one starts),
keyed on the session Key from the first SessionInfo.
"""

import unicodedata
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _normalize_circuit(name: str) -> str:
    """Circuit short name -> SVG basename: drop accents, spaces -> underscores."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_name.strip().replace(" ", "_")


class SessionInfoProcessor(Processor):
    """Extracts session metadata from SessionInfo and emits display topics."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._expected_key: Optional[int] = None
        # Accumulated raw fields (SessionInfo is delta-encoded — each field
        # arrives once and persists; this accumulation happens during the
        # linear preprocess pass, not on seek).
        self._session_type: str = ""
        self._session_name: str = ""
        self._session_number: int = 0
        self._session_status: str = ""
        self._gmt_offset: str = ""
        self._meeting_name: str = ""
        self._circuit: str = ""
        self._qualifying_part: int = 0
        # Last-emitted values per topic, for change-only emission.
        self._last_meeting_name: Optional[str] = None
        self._last_badge: Optional[str] = None
        self._last_circuit: Optional[str] = None
        self._last_session_info: Optional[dict] = None

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle)
        # qualifyingPart is emitted by SessionDataProcessor; it advances the
        # badge (Q -> Q1 -> Q2 -> Q3) and feeds the sessionInfo payload.
        self._bus.on("qualifyingPart", self._handle_qualifying_part)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        # Establish expected session key from first message that has one,
        # then ignore SessionInfo for any other session.
        session_key = data.get("Key")
        if session_key is not None:
            if self._expected_key is None:
                self._expected_key = session_key
            elif session_key != self._expected_key:
                return

        if "Type" in data:
            self._session_type = data["Type"]
        if "Name" in data:
            self._session_name = data["Name"]
        if "Number" in data and data["Number"] is not None:
            try:
                self._session_number = int(data["Number"])
            except (TypeError, ValueError):
                pass
        if "SessionStatus" in data:
            self._session_status = data["SessionStatus"]
        if "GmtOffset" in data:
            self._gmt_offset = data["GmtOffset"]
        meeting = data.get("Meeting")
        if isinstance(meeting, dict):
            if "Name" in meeting:
                self._meeting_name = meeting["Name"]
            circuit = meeting.get("Circuit")
            if isinstance(circuit, dict) and circuit.get("ShortName"):
                self._circuit = circuit["ShortName"]

        self._emit(clock_time)

    def _handle_qualifying_part(self, data: Any, clock_time: datetime) -> None:
        try:
            qp = int(data)
        except (TypeError, ValueError):
            return
        if qp == self._qualifying_part:
            return
        self._qualifying_part = qp
        self._emit(clock_time)

    def _compute_badge(self) -> str:
        name = self._session_name or ""
        initials = "".join(w[0] for w in name.split() if w).upper()
        if self._session_type == "Practice":
            return f"FP{self._session_number}" if self._session_number else "FP"
        if self._session_type == "Qualifying":
            return f"{initials}{self._qualifying_part}" if self._qualifying_part >= 1 else initials
        if self._session_type == "Race":
            return initials
        return ""

    def _emit(self, clock_time: datetime) -> None:
        # meetingName — own topic (the header title / weather radar use it).
        if self._meeting_name and self._meeting_name != self._last_meeting_name:
            self._last_meeting_name = self._meeting_name
            self._bus.emit("meetingName", self._meeting_name, clock_time)

        # trackCircuit — normalised SVG basename for the track map / weather.
        circuit = _normalize_circuit(self._circuit)
        if circuit and circuit != self._last_circuit:
            self._last_circuit = circuit
            self._bus.emit("trackCircuit", circuit, clock_time)

        # sessionBadge — computed display badge.
        badge = self._compute_badge()
        if badge and badge != self._last_badge:
            self._last_badge = badge
            self._bus.emit("sessionBadge", badge, clock_time)

        # sessionInfo — raw session metadata (no meetingName / badge).
        info = {
            "sessionType": self._session_type,
            "sessionName": self._session_name,
            "sessionNumber": self._session_number,
            "qualifyingPart": self._qualifying_part,
            "gmtOffset": self._gmt_offset,
            "sessionStatus": self._session_status,
        }
        if info != self._last_session_info:
            self._last_session_info = info
            self._bus.emit("sessionInfo", info, clock_time)

    # snapshot/restore/reset are unused (the DB latest-row-per-topic model
    # replaced the in-processor seek machinery); kept only to satisfy the
    # base ABC. Removed wholesale in the dead-snapshot-removal branch.
    def snapshot(self) -> dict[str, Any]:
        return {
            "expected_key": self._expected_key,
            "session_type": self._session_type,
            "session_name": self._session_name,
            "session_number": self._session_number,
            "session_status": self._session_status,
            "gmt_offset": self._gmt_offset,
            "meeting_name": self._meeting_name,
            "circuit": self._circuit,
            "qualifying_part": self._qualifying_part,
            "last_meeting_name": self._last_meeting_name,
            "last_badge": self._last_badge,
            "last_circuit": self._last_circuit,
            "last_session_info": self._last_session_info,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self._expected_key = state.get("expected_key")
        self._session_type = state.get("session_type", "")
        self._session_name = state.get("session_name", "")
        self._session_number = state.get("session_number", 0)
        self._session_status = state.get("session_status", "")
        self._gmt_offset = state.get("gmt_offset", "")
        self._meeting_name = state.get("meeting_name", "")
        self._circuit = state.get("circuit", "")
        self._qualifying_part = state.get("qualifying_part", 0)
        self._last_meeting_name = state.get("last_meeting_name")
        self._last_badge = state.get("last_badge")
        self._last_circuit = state.get("last_circuit")
        self._last_session_info = state.get("last_session_info")

    def reset(self) -> None:
        self.__init__(self._bus, self._session_type)
