"""
Session Info Processor — session identity metadata.

Subscribes to: SessionInfo
Emits: display:sessionInfo

Filters out SessionInfo messages from a different session (can happen when
the previous session's archive hasn't completed before the new one starts).
Uses the session Key from the first SessionInfo to identify the correct session.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class SessionInfoProcessor(Processor):
    """Extracts session metadata from SessionInfo messages."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._expected_key: Optional[int] = None
        self._session_type: str = ""
        self._session_name: str = ""
        self._session_status: str = ""
        self._gmt_offset: str = ""
        self._meeting_name: str = ""
        self._qualifying_part: int = 0

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle)
        # qualifyingPart is emitted by SessionDataProcessor; we merge it into
        # the sessionInfo payload so the latest sessionInfo row in the DB
        # always carries the full snapshot (otherwise get_state_at returns
        # only the most-recent partial emit and the header loses context).
        self._bus.on("qualifyingPart", self._handle_qualifying_part)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        # Establish expected session key from first message that has one
        session_key = data.get("Key")
        if session_key is not None:
            if self._expected_key is None:
                self._expected_key = session_key
            elif session_key != self._expected_key:
                return

        changed = False

        if "Type" in data:
            self._session_type = data["Type"]
            changed = True
        if "Name" in data:
            self._session_name = data["Name"]
            changed = True
        if "SessionStatus" in data:
            self._session_status = data["SessionStatus"]
            changed = True
        if "GmtOffset" in data:
            self._gmt_offset = data["GmtOffset"]
            changed = True
        meeting = data.get("Meeting")
        if isinstance(meeting, dict) and "Name" in meeting:
            self._meeting_name = meeting["Name"]
            changed = True

        if changed:
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

    def _emit(self, clock_time: datetime) -> None:
        self._bus.emit("sessionInfo", {
            "sessionType": self._session_type,
            "sessionName": self._session_name,
            "sessionStatus": self._session_status,
            "gmtOffset": self._gmt_offset,
            "meetingName": self._meeting_name,
            "qualifyingPart": self._qualifying_part,
        }, clock_time)
