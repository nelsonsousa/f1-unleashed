"""Heartbeat Processor — keeps the live leading edge advancing during quiet
periods.

The playback live edge is derived from ``MAX(offset_ms)`` over the processed
rows persisted in the transient DB. During pre-session (and any lull) the only
inbound F1 message is ``Heartbeat``, which is a RAW_F1_TOPIC and is therefore
dropped by ``preprocessor._capture_output`` — so no row is written and the edge
freezes even though time is passing and the server keeps receiving data.

This processor re-emits every ``Heartbeat`` as a persisted ``heartbeat`` topic.
Because the topic is not raw and its payload carries the beat's own timestamp
(so it never de-duplicates), each beat writes a row whose ``offset_ms`` reflects
the current live time — advancing ``MAX(offset_ms)`` and, through it, the
``state:clock`` duration the client scrubber follows.
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class HeartbeatProcessor(Processor):
    """Re-emit F1 Heartbeats as a persisted topic so the live edge advances."""

    def subscribe(self) -> None:
        self._bus.on("Heartbeat", self._on_heartbeat)

    def _on_heartbeat(self, data: Any, clock_time: datetime) -> None:
        self._bus.emit("heartbeat", {"utc": clock_time.isoformat()}, clock_time)
