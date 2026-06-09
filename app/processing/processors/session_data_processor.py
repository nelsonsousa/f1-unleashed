# DEPRECATED — marked for deletion (processor redesign). qualifyingPart will
# fold into session_info; sessionStatus is read from raw SessionStatus. Delete
# once consumers are rehomed and the UI is revised. See
# memory/processor_redesign_plan.md.
"""
Session Data Processor — session status and qualifying parts.

Subscribes to: SessionData
Emits:
  - event              SessionStatus Started/Finished
  - sessionInfo        Updated with qualifyingPart when QualifyingPart changes

Filters out entries with timestamps more than 1h before the first
valid SessionData message (stale data from previous sessions).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_utc(utc_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class SessionDataProcessor(Processor):
    """Processes SessionData for status changes and qualifying parts."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._first_valid_time: Optional[datetime] = None
        self._qualifying_part: int = 0
        self._session_status: Optional[str] = None
        self._finalised = False

    @property
    def finalised(self) -> bool:
        """True after SessionStatus=Finalised — processors should stop collecting."""
        return self._finalised

    def subscribe(self) -> None:
        self._bus.on("SessionData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        # Process StatusSeries entries
        status_series = data.get("StatusSeries")
        if isinstance(status_series, (dict, list)):
            items = status_series.values() if isinstance(status_series, dict) else status_series
            for entry in items:
                if not isinstance(entry, dict):
                    continue

                utc = _parse_utc(entry.get("Utc", ""))
                if utc and not self._is_valid_time(utc):
                    continue

                ss = entry.get("SessionStatus")
                if ss and ss != self._session_status:
                    self._session_status = ss
                    if ss == "Finalised":
                        self._finalised = True
                    # Emit the status change so downstream processors can
                    # react (e.g. TrackStatusProcessor uses Started as the
                    # authoritative race-GREEN trigger). Chained-topic
                    # pattern, same as `qualifyingPart` below.
                    self._bus.emit("sessionStatus", ss, clock_time)

        # Process Series entries (QualifyingPart)
        series = data.get("Series")
        if isinstance(series, (dict, list)):
            items = series.values() if isinstance(series, dict) else series
            for entry in items:
                if not isinstance(entry, dict):
                    continue

                utc = _parse_utc(entry.get("Utc", ""))
                if utc and not self._is_valid_time(utc):
                    continue

                qp = entry.get("QualifyingPart")
                # Only 1/2/3 are real segments — QualifyingPart=0 means
                # qualifying hasn't started yet (pre-session) or a post-
                # session reset. Don't emit or update state from those.
                if qp is not None and 1 <= int(qp) <= 3 and qp != self._qualifying_part:
                    self._qualifying_part = int(qp)
                    # SessionInfoProcessor listens to this and rolls it into
                    # the merged sessionInfo payload, so we don't emit
                    # sessionInfo here (would overwrite the merged snapshot
                    # in the DB with a partial payload).
                    self._bus.emit("qualifyingPart", self._qualifying_part, clock_time)

    def _is_valid_time(self, utc: datetime) -> bool:
        """Filter out entries with timestamps >1h before first valid message."""
        if self._first_valid_time is None:
            self._first_valid_time = utc
            return True
        cutoff = self._first_valid_time - timedelta(hours=1)
        return utc >= cutoff
