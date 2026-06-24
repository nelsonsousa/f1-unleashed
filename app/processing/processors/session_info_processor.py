"""
Session Info Processor — session identity metadata + display badge.

Subscribes to: SessionInfo, SessionData
Emits (each self-contained — restore = latest row per topic):
  meetingName    Event/meeting name string (e.g. "Monaco Grand Prix")
  sessionBadge   Display badge string (FP1 / Q / Q1 / SQ2 / S / R …)
  trackCircuit   Circuit name normalised for the SVG basename
                 (accents dropped, spaces -> underscores; "Monte_Carlo")
  sessionInfo    Raw session metadata: sessionType, sessionName,
                 sessionNumber, qualifyingPart, gmtOffset, sessionStatus
  qualifyingPart Chained topic (1/2/3) consumed by driver_status + standings
                 to track the current qualifying segment.

The badge mapping (formerly in header.js) is computed here:
  Practice    -> "FP" + session Number          (FP1, FP2, FP3)
  Qualifying  -> initials(Name) [+ qualifyingPart if >= 1]
                 ("Qualifying" -> Q/Q1/Q2/Q3; "Sprint Qualifying" -> SQ/SQ1…)
  Race        -> initials(Name)                  ("Race" -> R; "Sprint" -> S)

The live SessionStatus (Started/Finished/Finalised) and QualifyingPart come
from SessionData (StatusSeries / Series); SessionInfo carries the static
identity metadata. Filters out SessionInfo from a different session (keyed on
the first session Key) and SessionData entries >1h before the first valid one
(stale rows from a previous session whose archive overlapped).
"""

import unicodedata
from datetime import datetime, timedelta
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_utc(utc_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


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
        self._start_date: str = ""        # scheduled session start (local; pair with gmtOffset)
        self._meeting_name: str = ""
        self._circuit: str = ""
        self._qualifying_part: int = 0
        # Stale-row guard for SessionData (entries >1h before the first valid
        # one come from a previous session whose archive overlapped).
        self._first_valid_time: Optional[datetime] = None
        # Last-emitted values per topic, for change-only emission.
        self._last_meeting_name: Optional[str] = None
        self._last_badge: Optional[str] = None
        self._last_circuit: Optional[str] = None
        self._last_session_info: Optional[dict] = None

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle)
        # SessionData carries the live SessionStatus + QualifyingPart.
        self._bus.on("SessionData", self._handle_session_data)

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
        if "StartDate" in data:
            self._start_date = data["StartDate"]
        meeting = data.get("Meeting")
        if isinstance(meeting, dict):
            if "Name" in meeting:
                self._meeting_name = meeting["Name"]
            circuit = meeting.get("Circuit")
            if isinstance(circuit, dict) and circuit.get("ShortName"):
                self._circuit = circuit["ShortName"]

        self._emit(clock_time)

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        changed = False

        # StatusSeries -> live SessionStatus (Started / Finished / Finalised).
        status_series = data.get("StatusSeries")
        if isinstance(status_series, (dict, list)):
            items = status_series.values() if isinstance(status_series, dict) else status_series
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                if not self._is_valid_time(_parse_utc(entry.get("Utc", ""))):
                    continue
                ss = entry.get("SessionStatus")
                if ss and ss != self._session_status:
                    self._session_status = ss
                    changed = True

        # Series -> QualifyingPart. Only 1/2/3 are real segments
        # (0 = pre-session / post-session reset). Emit the chained topic
        # (consumed by driver_status + standings) on change.
        series = data.get("Series")
        if isinstance(series, (dict, list)):
            items = series.values() if isinstance(series, dict) else series
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                if not self._is_valid_time(_parse_utc(entry.get("Utc", ""))):
                    continue
                qp = entry.get("QualifyingPart")
                if qp is not None and 1 <= int(qp) <= 3 and int(qp) != self._qualifying_part:
                    self._qualifying_part = int(qp)
                    self._bus.emit("qualifyingPart", self._qualifying_part, clock_time)
                    changed = True

        if changed:
            self._emit(clock_time)

    def _is_valid_time(self, utc: Optional[datetime]) -> bool:
        """Filter out entries >1h before the first valid SessionData time."""
        if utc is None:
            return True
        if self._first_valid_time is None:
            self._first_valid_time = utc
            return True
        return utc >= self._first_valid_time - timedelta(hours=1)

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
            "startDate": self._start_date,
            "sessionStatus": self._session_status,
        }
        if info != self._last_session_info:
            self._last_session_info = info
            self._bus.emit("sessionInfo", info, clock_time)
