"""RED GATE — Trello card yVV6NNl9 ("lap_timing_processor: post-session resend
overwrites correct final lap times (13/22 cars, 12 lose a personal best)").

Evidence: docs/artifacts/2026-07-29-027-processor-cardinality-mapping/data-investigation.md
section 10, traced end-to-end on car 31 of a Spa Qualifying capture.

Defect 1 (primary): `_advance` drains `_pending` (or applies `bundled_ll`) onto
`_completed(new_nol)` whenever `new_c >= 1`, without checking the lap counter
actually advanced. A post-session full-state resend re-sends `NumberOfLaps`
unchanged (`prev_c == new_c`), so a stale `_pending` value silently overwrites
the already-completed, correct lap time (and its personalBest flag).

Defect 2 (independent, smaller): `_pending` is a single dict slot keyed by
driver number. A second standalone `LastLapTime` arriving before the next
`NumberOfLaps` advance silently displaces the first — it is gone, with no
trace, and the wrong value later lands against a completed lap.

Both tests below are written against the CURRENT (unfixed) code and are
expected to FAIL for the reasons stated in each docstring.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.lap_timing_processor import LapTimingProcessor


class LapTimingResendOverwriteTests(unittest.TestCase):
    def _make(self, session_type="qualifying"):
        bus = SessionMessageBus()
        proc = LapTimingProcessor(bus, session_type)
        proc.subscribe()
        emitted = {}

        def _capture(num):
            def _cb(d, ct):
                emitted[num] = d
            return _cb

        for num in ("31", "7"):
            bus.on(f"driverLaps:{num}", _capture(num))
        ct = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        return bus, emitted, ct

    def test_post_session_resend_does_not_overwrite_completed_lap(self):
        """Defect 1 — mirrors car 31 exactly (data-investigation.md §10):

        NoL:6 + LastLapTime 1:47.801 (PB)  -> lap 5 = 1:47.801, PB.
        standalone LastLapTime 3:01.108    -> held pending (in-progress lap 6).
        standalone LastLapTime 4:33.917    -> displaces the held pending value.
        post-session resend, NoL:6 UNCHANGED -> must NOT touch lap 5's time.

        Against the unfixed code, the tail of `_advance` unconditionally
        drains `_pending` onto `_completed(new_c)` because it only checks
        `new_c >= 1`, not that the counter actually moved. Lap 5's correct,
        personal-best time gets overwritten with the stale in-progress value.
        """
        bus, emitted, ct = self._make("qualifying")
        num = "31"

        # NoL:6 (completed lap = 6-1 = 5), bundled LastLapTime, PB.
        bus.emit("TimingData", {"Lines": {num: {
            "NumberOfLaps": 6,
            "LastLapTime": {"Value": "1:47.801", "PersonalFastest": True},
        }}}, ct)
        last = emitted[num]["lastLap"]
        self.assertEqual(last["lap"], 5)
        self.assertEqual(last["time"], "1:47.801")
        self.assertTrue(last["personalBest"])

        # Two standalone LastLapTimes for the still in-progress lap 6 — no
        # NumberOfLaps in either message.
        bus.emit("TimingData", {"Lines": {num: {
            "LastLapTime": {"Value": "3:01.108"},
        }}}, ct)
        bus.emit("TimingData", {"Lines": {num: {
            "LastLapTime": {"Value": "4:33.917"},
        }}}, ct)

        # Post-session full-state resend: NumberOfLaps repeats the SAME value,
        # no bundled LastLapTime. This is not a new lap advance.
        bus.emit("TimingData", {"Lines": {num: {
            "NumberOfLaps": 6,
        }}}, ct)

        last = emitted[num]["lastLap"]
        self.assertEqual(
            last["time"], "1:47.801",
            "resend with an unchanged NumberOfLaps must not overwrite the "
            "already-completed lap 5 time with a stale in-progress value",
        )
        self.assertEqual(last["lap"], 5)
        self.assertTrue(
            last["personalBest"],
            "the personal-best flag on the completed lap must survive a "
            "no-op resend",
        )

    def test_second_standalone_before_advance_is_not_lost(self):
        """Defect 2 — a second standalone LastLapTime arriving before the
        next genuine NumberOfLaps advance must not silently displace/lose
        the first held value. Two GENUINE advances follow (not a resend),
        isolating this from Defect 1: each advance should consume one held
        value, in order, so both survive and land on the correct lap.

        Against the unfixed code, `_pending[num] = ll` in `_standalone`
        unconditionally overwrites any already-held value, so only the
        second standalone value ("1:32.000") survives — it wrongly lands on
        lap 3 (the first advance), and lap 4 (the second advance) is left
        with no time at all, since `_pending` is now empty.
        """
        bus, emitted, ct = self._make("qualifying")
        num = "7"

        # NoL:3 (completed lap = 3-1 = 2), bundled LastLapTime -> lap 2 set.
        bus.emit("TimingData", {"Lines": {num: {
            "NumberOfLaps": 3,
            "LastLapTime": {"Value": "1:30.000"},
        }}}, ct)

        # Two standalone LastLapTimes for the in-progress lap 3, no advance
        # between them.
        bus.emit("TimingData", {"Lines": {num: {
            "LastLapTime": {"Value": "1:31.000"},
        }}}, ct)
        bus.emit("TimingData", {"Lines": {num: {
            "LastLapTime": {"Value": "1:32.000"},
        }}}, ct)

        # Genuine advance #1: NoL 3 -> 4 (completed lap 3). Should consume
        # the FIRST held value (1:31.000), not the second.
        bus.emit("TimingData", {"Lines": {num: {"NumberOfLaps": 4}}}, ct)
        self.assertEqual(
            emitted[num]["lastLap"]["time"], "1:31.000",
            "the first standalone LastLapTime must not have been silently "
            "displaced by the second",
        )
        self.assertEqual(emitted[num]["lastLap"]["lap"], 3)

        # Genuine advance #2: NoL 4 -> 5 (completed lap 4). Should consume
        # the SECOND held value (1:32.000), which must still be available.
        bus.emit("TimingData", {"Lines": {num: {"NumberOfLaps": 5}}}, ct)
        self.assertEqual(
            emitted[num]["lastLap"]["time"], "1:32.000",
            "the second standalone LastLapTime must still be available for "
            "the next genuine lap advance",
        )
        self.assertEqual(emitted[num]["lastLap"]["lap"], 4)


class LapTimingChangedBranchCoverageTests(unittest.TestCase):
    """Not regression cases — these exist purely to reach the remaining
    branches of the fix in `_advance`/`_standalone` (critical-path file,
    100% branch coverage on changed lines per CLAUDE.local.md)."""

    def _make(self, session_type="qualifying"):
        bus = SessionMessageBus()
        proc = LapTimingProcessor(bus, session_type)
        proc.subscribe()
        emitted = {}
        bus.on("driverLaps:9", lambda d, ct: emitted.__setitem__("9", d))
        ct = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)
        bus.emit("SessionStatus", {"Status": "Started"}, ct)
        return bus, emitted, ct

    def test_genuine_advance_with_no_time_data_falls_through(self):
        """A genuine advance (new_c > prev_c) with neither a bundled
        LastLapTime nor anything held in `_pending` must not raise or set a
        time — it just creates the (still timeless) lap slot and returns.
        Covers the `new_c > prev_c` branch where the inner bundled_ll/pending
        checks are both false."""
        bus, emitted, ct = self._make()
        bus.emit("TimingData", {"Lines": {"9": {"NumberOfLaps": 2}}}, ct)
        self.assertIsNone(emitted["9"]["lastLap"])

    def test_out_lap_standalone_time_is_queued_not_applied_immediately(self):
        """P/Q: a standalone LastLapTime while the driver is still on lap 1
        (the out lap) must be queued (not applied to lap 1, which would
        mis-assign the best to an out lap) — covers the `not self._is_race
        and c == 1` branch in `_standalone` with the new list-append form."""
        bus, emitted, ct = self._make()
        # NoL:2 (completed lap = 1, the out lap), no bundled time.
        bus.emit("TimingData", {"Lines": {"9": {"NumberOfLaps": 2}}}, ct)
        # Standalone flying-lap time arrives before NoL confirms lap 2's end.
        bus.emit("TimingData", {"Lines": {"9": {
            "LastLapTime": {"Value": "1:33.500"},
        }}}, ct)
        # Not yet applied to lap 1 (the out lap).
        self.assertIsNone(emitted["9"]["lastLap"])
        # The next genuine advance drains the queued value onto lap 2.
        bus.emit("TimingData", {"Lines": {"9": {"NumberOfLaps": 3}}}, ct)
        self.assertEqual(emitted["9"]["lastLap"]["lap"], 2)
        self.assertEqual(emitted["9"]["lastLap"]["time"], "1:33.500")


if __name__ == "__main__":
    unittest.main()
