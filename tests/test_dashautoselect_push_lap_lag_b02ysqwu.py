"""Trello B02ySqwU -- "auto-select picks out-lap drivers instead of push-lap drivers,
inverted from intent" -- red-gate investigation.

Reported symptom: Q1/Q2 auto-select highlights the driver about to FINISH their OUT lap
(rather than a driver on a genuine PUSH lap), and un-selects them the instant they cross
S/F and actually start pushing.

Reported hypothesis: `driverLapClassification`'s `type` lags by one lap during an
in-progress lap -- an OUT-lap driver still reads `type=PUSH` from their PREVIOUS flying
lap, so their rising track % floats them to the top of the push-lap candidate list right
as they approach S/F; the instant the real push lap starts, classification updates to
the just-completed OUT lap and the PUSH flag flips False, dropping them.

This test is the red-gate check for that hypothesis, run against the CURRENT,
unmodified pipeline (docs/artifacts/2026-08-19-070-wb5-autoselect-push-lap-focus/
implementation-plan.md has the full trace). It reconstructs the exact scenario the card
describes -- an out-lap driver approaching S/F at high track %, alongside a driver
genuinely mid-push -- using the REAL processor chain
(DriverStatusProcessor -> LapTimingProcessor -> LapClassificationProcessor ->
DashboardAutoSelectProcessor) wired together, with a raw-TimingData message split that
matches every out-lap-to-push-lap transition found in this repo's captured sessions
(`PitOut` clears in its own message, strictly before `NumberOfLaps` increments to the
push lap -- verified across 832 transitions in backups/polluted-captures/*.jsonl).

RESULT (against the current, unmodified code): the hypothesis does NOT reproduce.
`LapClassificationProcessor` no longer subscribes to raw `TimingData` (it consumes
`driverStatus:{num}`/`driverLaps:{num}` via wildcard); `DriverStatusProcessor` is
registered before `LapTimingProcessor` in `preprocessor.py`, and `SessionMessageBus`
dispatch is synchronous/re-entrant, so for any single incoming TimingData tick the
status update always lands before the lap-boundary reclassification that reads it. The
OUT-lap driver's PUSH flag is False for the whole out lap and flips True the instant
the push lap starts -- matching intent, not the reported inversion.

Per .claude/rules/workflow.md ("If [the red-gate test] passes, stop -- the test does not
reproduce the bug"): no fix is made for B02ySqwU. This test is kept as a standing
regression guard for the (already correct) behavior, since it exercises the exact
acceptance criteria the card describes.
"""
import unittest
from datetime import datetime, timedelta

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.dashboard_autoselect_processor import DashboardAutoSelectProcessor
from app.processing.processors.driver_status_processor import DriverStatusProcessor
from app.processing.processors.lap_classification_processor import LapClassificationProcessor
from app.processing.processors.lap_timing_processor import LapTimingProcessor

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class _Harness:
    """Wires the real processor chain the bug report spans, on one bus."""

    def __init__(self, session_type="qualifying"):
        self.bus = SessionMessageBus()
        self.driver_status = DriverStatusProcessor(self.bus, session_type)
        self.lap_timing = LapTimingProcessor(self.bus, session_type)
        self.lap_class = LapClassificationProcessor(self.bus, session_type)
        self.auto_select = DashboardAutoSelectProcessor(self.bus, session_type)
        for p in (self.driver_status, self.lap_timing, self.lap_class, self.auto_select):
            p.subscribe()
        self.picks = []
        self.bus.on("dashAutoSelect", lambda d, ct: self.picks.append((ct, list(d))))
        self.cls_log = []
        self.bus.on("*", self._wild)

    def _wild(self, topic, data, clock_time):
        if topic.startswith("driverLapClassification:"):
            self.cls_log.append((clock_time, topic.split(":", 1)[1], dict(data)))


class PushLapLagB02ySqwUTest(unittest.TestCase):

    def test_out_lap_driver_never_shown_as_push_candidate(self):
        """A driver approaching S/F on an OUT lap must never read as a PUSH
        candidate, however close their track % gets to 100."""
        h = _Harness()
        bus = h.bus

        bus.emit("SessionStatus", {"Status": "Started"}, _ts(0))
        bus.emit("qualifyingPart", 1, _ts(0))
        bus.emit("sessionInfo", {"sessionStatus": "Started"}, _ts(0))
        bus.emit("standings", {"drivers": [
            {"num": "44", "position": 3},   # mid-push comparison driver
            {"num": "7", "position": 16},   # the out-lap driver under test
        ]}, _ts(0))

        # Driver 44: genuinely mid-push (lap 5, no PitOut/InPit ever set).
        bus.emit("TimingData", {"Lines": {"44": {"NumberOfLaps": 5}}}, _ts(1))
        bus.emit("position", {"44": [0, 0, 55.0]}, _ts(2))

        # Driver 7: exits the pits onto an out lap (lap 3).
        bus.emit("TimingData", {"Lines": {"7": {"PitOut": True, "NumberOfLaps": 3}}}, _ts(1))
        for t, pct in ((3, 10.0), (10, 40.0), (18, 75.0), (23, 97.0)):
            bus.emit("position", {"7": [0, 0, pct]}, _ts(t))
            # Driver 7 must not be a push candidate at ANY point of the out lap,
            # regardless of how close to S/F their track % is.
            self.assertFalse(
                h.auto_select._push.get("7"),
                f"driver 7 read as PUSH mid-out-lap at track%={pct} (t={t}s) -- "
                "the reported PUSH-flag lag",
            )

        self.assertEqual(
            h.auto_select._push_by_dp(), ["44"],
            "only the genuinely-pushing driver should be a candidate while "
            "driver 7 is still finishing their out lap",
        )

    def test_selection_survives_the_out_lap_to_push_lap_crossing(self):
        """A driver who WAS an out-lap driver and starts a genuine push lap
        must become (and then remain) a push candidate -- not get dropped the
        instant they cross S/F, and not require a second lap to "catch up"."""
        h = _Harness()
        bus = h.bus

        bus.emit("SessionStatus", {"Status": "Started"}, _ts(0))
        bus.emit("qualifyingPart", 1, _ts(0))
        bus.emit("sessionInfo", {"sessionStatus": "Started"}, _ts(0))
        bus.emit("standings", {"drivers": [{"num": "7", "position": 16}]}, _ts(0))

        bus.emit("TimingData", {"Lines": {"7": {"PitOut": True, "NumberOfLaps": 3}}}, _ts(1))
        bus.emit("position", {"7": [0, 0, 97.0]}, _ts(23))
        self.assertFalse(h.auto_select._push.get("7"))

        # Crossing S/F: matches every transition found in this repo's captures --
        # PitOut clears in its own message, strictly before NumberOfLaps increments.
        bus.emit("TimingData", {"Lines": {"7": {"PitOut": False}}}, _ts(24))
        bus.emit("TimingData", {"Lines": {"7": {"NumberOfLaps": 4}}}, _ts(25))
        bus.emit("position", {"7": [0, 0, 0.5]}, _ts(25))

        self.assertTrue(
            h.auto_select._push.get("7"),
            "driver 7 should read as PUSH the instant the new (timed) lap starts",
        )
        cls_types = [t for _, num, d in h.cls_log if num == "7" for t in [d.get("type")]]
        self.assertEqual(
            cls_types, ["OUT", "PUSH"],
            f"expected exactly one OUT->PUSH transition at the S/F crossing, got {cls_types}",
        )

    def test_bundled_pitout_false_and_lap_increment_in_same_message(self):
        """Some feeds bundle PitOut:False with the NumberOfLaps increment in a
        single message rather than two -- must resolve to PUSH just the same."""
        h = _Harness()
        bus = h.bus

        bus.emit("SessionStatus", {"Status": "Started"}, _ts(0))
        bus.emit("qualifyingPart", 1, _ts(0))
        bus.emit("sessionInfo", {"sessionStatus": "Started"}, _ts(0))
        bus.emit("standings", {"drivers": [{"num": "7", "position": 16}]}, _ts(0))

        bus.emit("TimingData", {"Lines": {"7": {"PitOut": True, "NumberOfLaps": 3}}}, _ts(1))
        bus.emit("position", {"7": [0, 0, 97.0]}, _ts(23))
        self.assertFalse(h.auto_select._push.get("7"))

        bus.emit("TimingData", {"Lines": {"7": {"PitOut": False, "NumberOfLaps": 4}}}, _ts(24))
        bus.emit("position", {"7": [0, 0, 0.5]}, _ts(24))

        self.assertTrue(h.auto_select._push.get("7"))


if __name__ == "__main__":
    unittest.main()
