"""
Playback Clock for session replay.

Maintains current time, speed (playback 1x-10x; the setter clamps to 0.1x-60x
as a hard bound), play/pause state.
The SessionEngine calls tick() on each iteration to advance the clock
and determine which messages should be dispatched.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import time


class ClockState(str, Enum):
    PAUSED = "paused"
    PLAYING = "playing"


class PlaybackClock:
    """UTC-based playback clock with speed control."""

    def __init__(self, start_time: datetime, display_delay_ms: int = 2000):
        # Session timeline
        self._start_time = start_time
        self._display_delay = timedelta(milliseconds=display_delay_ms)

        # Clock starts at start_time + display_delay so display time = start_time
        self._current_time = start_time + self._display_delay

        # Playback control
        self._state = ClockState.PAUSED
        self._speed = 1.0
        self._last_tick_real: Optional[float] = None  # time.monotonic() value

    @property
    def current_time(self) -> datetime:
        """Current clock time (UTC)."""
        return self._current_time

    @property
    def display_time(self) -> datetime:
        """Display time = clock time minus delay."""
        return self._current_time - self._display_delay

    @property
    def start_time(self) -> datetime:
        """Session start time."""
        return self._start_time

    @property
    def offset_seconds(self) -> float:
        """Current offset from start in seconds (based on display time)."""
        return max(0.0, (self.display_time - self._start_time).total_seconds())

    @property
    def state(self) -> ClockState:
        return self._state

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float):
        # Flush elapsed time at old speed before changing
        if self._state == ClockState.PLAYING and self._last_tick_real is not None:
            now = time.monotonic()
            elapsed = now - self._last_tick_real
            self._current_time += timedelta(seconds=elapsed * self._speed)
            self._last_tick_real = now
        self._speed = max(0.1, min(value, 60.0))

    def play(self) -> None:
        """Start or resume playback."""
        if self._state == ClockState.PLAYING:
            return
        self._state = ClockState.PLAYING
        self._last_tick_real = time.monotonic()

    def pause(self) -> None:
        """Pause playback."""
        if self._state == ClockState.PAUSED:
            return
        # Flush any elapsed time
        if self._last_tick_real is not None:
            now = time.monotonic()
            elapsed = now - self._last_tick_real
            self._current_time += timedelta(seconds=elapsed * self._speed)
        self._state = ClockState.PAUSED
        self._last_tick_real = None

    def tick(self) -> datetime:
        """Advance the clock based on real elapsed time. Returns current clock time."""
        if self._state == ClockState.PLAYING and self._last_tick_real is not None:
            now = time.monotonic()
            elapsed = now - self._last_tick_real
            self._last_tick_real = now
            self._current_time += timedelta(seconds=elapsed * self._speed)
        return self._current_time

    def seek_to(self, display_time: datetime) -> None:
        """Seek to a specific display time."""
        self._current_time = display_time + self._display_delay
        if self._state == ClockState.PLAYING:
            self._last_tick_real = time.monotonic()

    def seek_to_offset(self, offset_seconds: float) -> None:
        """Seek to a specific offset from session start."""
        target = self._start_time + timedelta(seconds=offset_seconds)
        self.seek_to(target)
