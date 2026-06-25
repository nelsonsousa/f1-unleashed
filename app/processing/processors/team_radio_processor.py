"""Team Radio Processor (card 8) — playback events for downloaded clips.

Subscribes to: TeamRadio
Emits: teamRadio  { num, file }   at the clip's broadcast Utc, so the client
       plays it time-aligned and ducks the commentary while it runs.

The initial subscribe carries a LIST of pre-session BACKLOG clips (older
broadcasts whose Utc is the snapshot time, not the real airing time) — those are
still downloaded (live_capture / team_radio) but are NOT emitted for playback.
Incremental updates arrive as a DICT, one clip at a time as it airs live —
those are emitted at their Utc. Deduped by Path.
"""

import os
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_utc(s: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


class TeamRadioProcessor(Processor):
    """Emit a `teamRadio` play event per live radio clip, at its broadcast Utc."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._seen: set[str] = set()

    def subscribe(self) -> None:
        self._bus.on("TeamRadio", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        caps = data.get("Captures")
        # LIST = initial subscribe backlog (pre-session) → seen-only, no playback.
        # DICT = incremental live clips → emit at their Utc.
        backlog = isinstance(caps, list)
        if backlog:
            items = caps
        elif isinstance(caps, dict):
            items = list(caps.values())
        else:
            return
        for c in items:
            if not isinstance(c, dict):
                continue
            path = c.get("Path")
            if not path or path in self._seen:
                continue
            self._seen.add(path)
            if backlog:
                continue
            utc = _parse_utc(c.get("Utc")) or clock_time
            self._bus.emit("teamRadio", {
                "num": str(c.get("RacingNumber") or ""),
                "file": os.path.basename(path),
            }, utc)
