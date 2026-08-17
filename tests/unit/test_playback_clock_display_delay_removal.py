"""Regression coverage for `PlaybackClock`'s `display_delay_ms` deletion
(2026-08-17-047 WB-1 resume, item 4; file-impact-map.md §4;
architecture-plan.md §A.5.1).

No test anywhere constructed a `PlaybackClock` directly or asserted on
`.display_time`/`.offset_seconds` before this task (file-impact-map.md §4:
"grep across `tests/` for `PlaybackClock`, `display_delay_ms`,
`.current_time`, `.display_time` returns no hits at all"). The deletion
itself was zero-risk BY PROOF (`display_delay_ms` always cancelled itself
out: `_current_time` was initialized at `start_time + display_delay` and
`display_time` immediately subtracted the same delay back out), but nothing
closed the loop by actually asserting the post-deletion behaviour matches
what the proof claims. This file is that assertion.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.clock import PlaybackClock, ClockState


class DisplayTimeMatchesStartTimeAtConstruction(unittest.TestCase):
    def test_display_time_equals_start_time_immediately_after_construction(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        self.assertEqual(clock.display_time, start)
        self.assertEqual(clock.current_time, start)

    def test_offset_seconds_is_zero_immediately_after_construction(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        self.assertEqual(clock.offset_seconds, 0.0)

    def test_constructor_accepts_no_second_positional_argument(self):
        """`PlaybackClock`'s ONLY constructor call site (session.py:291) has
        always passed a single positional arg -- confirm the signature no
        longer accepts (or silently ignores) a `display_delay_ms` at all."""
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(TypeError):
            PlaybackClock(start, 5000)  # noqa: the old 2-arg call shape


class SeekToRoundTripsExactly(unittest.TestCase):
    """Before the deletion, `seek_to` added `_display_delay` back on
    (`display_time + self._display_delay`) and `display_time` subtracted it
    again -- a round trip that always cancelled. Post-deletion there is no
    delay to add/subtract at all; `seek_to(display_time)` must set
    `display_time` to EXACTLY the requested value, not off by any constant."""

    def test_seek_to_sets_display_time_to_exactly_the_requested_value(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        target = start + timedelta(minutes=37, seconds=12)
        clock.seek_to(target)
        self.assertEqual(clock.display_time, target)
        self.assertEqual(clock.current_time, target)

    def test_seek_to_offset_zero_returns_to_start_time(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        clock.seek_to(start + timedelta(minutes=10))  # move away first
        clock.seek_to_offset(0)
        self.assertEqual(clock.display_time, start)
        self.assertEqual(clock.offset_seconds, 0.0)

    def test_seek_to_offset_matches_requested_offset(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        clock.seek_to_offset(90.0)
        self.assertEqual(clock.display_time, start + timedelta(seconds=90))
        self.assertAlmostEqual(clock.offset_seconds, 90.0, places=6)


class TickAdvancesDisplayTimeDirectly(unittest.TestCase):
    """Before the deletion, playback advanced `_current_time` (which sat
    `display_delay` AHEAD of `display_time`) and `display_time` always
    subtracted the same constant back out. Post-deletion, `_current_time` IS
    `display_time` -- confirm `tick()` while PLAYING advances `display_time`
    by exactly the elapsed real time (no constant offset lurking)."""

    def test_tick_while_playing_advances_display_time_by_speed_times_elapsed(self):
        import time as time_module
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        clock.speed = 2.0
        clock.play()
        # Simulate elapsed real time by directly rewinding the monotonic
        # anchor rather than sleeping — deterministic, no flakiness.
        clock._last_tick_real = time_module.monotonic() - 3.0
        result = clock.tick()
        # 3 real seconds * 2x speed = 6 simulated seconds advanced.
        expected = start + timedelta(seconds=6)
        self.assertAlmostEqual(
            (result - expected).total_seconds(), 0.0, places=1)
        self.assertAlmostEqual(
            (clock.display_time - expected).total_seconds(), 0.0, places=1)

    def test_tick_while_paused_does_not_advance(self):
        start = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        clock = PlaybackClock(start)
        self.assertEqual(clock.state, ClockState.PAUSED)
        result = clock.tick()
        self.assertEqual(result, start)
        self.assertEqual(clock.display_time, start)


if __name__ == "__main__":
    unittest.main()
