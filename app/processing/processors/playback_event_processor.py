# DEPRECATED — marked for deletion (processor redesign). Scrubber markers will
# come from track/session status instead. Delete once the UI is revised and no
# client consumes its topics. See memory/processor_redesign_plan.md.
"""
Playback Event Processor — scrubber markers for the playback timeline.

Emits playbackEvent + event markers. Listens to TrackStatusProcessor's
"event" topic via the preprocessor's wildcard. Also emits:
  - sessionStart / sessionEnd (preprocessor-driven)
  - preStart2min  — race + sprint only; 2 min before scheduled lights-out
                    icon, so the user can jump straight to the pre-show.
  - audioStart    — first audible moment in commentary.aac (computed
                    via ffmpeg silencedetect), with a speaker icon.

Scheduled-start filter: track_status events that fire BEFORE the
session's scheduled start are NOT marked on the scrubber unless an
RCM has moved the start time earlier. This keeps pre-session noise
(implicit-GREEN at pit exit open, etc.) off the scrubber while still
showing the real race-start. See `set_effective_start_utc`.
"""

from datetime import datetime, timedelta
from typing import Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class PlaybackEventProcessor(Processor):
    """Emits playbackEvent messages for timeline markers."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._first_emitted = False
        # UTC moment at/after which `event` topics are allowed onto
        # the scrubber. Set by the preprocessor from SessionInfo at
        # init. The brought-forward RCM (rare) shifts this earlier.
        self._effective_start_utc: Optional[datetime] = None
        self._pre_start_emitted = False

    def subscribe(self) -> None:
        # The track_status "event" topic is captured by the
        # preprocessor's wildcard; we just need to watch it here so we
        # can decide whether to drop it (see filter_event).
        pass

    def set_effective_start_utc(self, ts: datetime) -> None:
        """Set or shift the threshold below which scrubber events are
        suppressed. Called once at init with the scheduled start, and
        again if an RCM moves the session forward."""
        if self._effective_start_utc is None or ts < self._effective_start_utc:
            self._effective_start_utc = ts

    def filter_event(self, clock_time: datetime) -> bool:
        """True iff an `event` at this clock_time should be SUPPRESSED
        from the scrubber. False = keep. Used by the preprocessor's
        _capture_output for the 'event' topic."""
        if self._effective_start_utc is None:
            return False
        return clock_time < self._effective_start_utc

    def emit_session_start(self, clock_time: datetime) -> None:
        """Called by preprocessor on first message."""
        if not self._first_emitted:
            self._first_emitted = True
            self._bus.emit("playbackEvent", "sessionStart", clock_time)

    def emit_session_end(self, clock_time: datetime) -> None:
        """Called by preprocessor on last message."""
        self._bus.emit("playbackEvent", "sessionEnd", clock_time)

    def maybe_emit_pre_start(self, capture_first_utc: datetime) -> None:
        """Place a pre-session marker on the scrubber.

        Per SME 2026-06-06:
          - Practice + Qualifying: NO pre-session marker (the first
            scrubber-worthy event is session start itself, handled by
            `emit_session_start`).
          - Race + Sprint: marker placed at `scheduled - 2 min`
            (= `preStart2min`); used by the non-linear scrubber as
            the start of the "interesting" middle section.

        Idempotent.
        """
        if self._pre_start_emitted or self._effective_start_utc is None:
            return
        st = (self._session_type or "").lower()
        if st not in ("race", "sprint"):
            return
        target = self._effective_start_utc - timedelta(minutes=2)
        if target < capture_first_utc:
            target = capture_first_utc
        self._pre_start_emitted = True
        self._bus.emit("playbackEvent", "preStart2min", target)

    def emit_audio_start(self, clock_time: datetime) -> None:
        """Place an `audioStart` marker at the first audible moment in
        the captured commentary. Caller (preprocessor) computes it via
        ffmpeg silencedetect once the capture is complete."""
        self._bus.emit("playbackEvent", "audioStart", clock_time)
