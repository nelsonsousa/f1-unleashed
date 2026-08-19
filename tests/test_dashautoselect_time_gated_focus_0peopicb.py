"""Trello 0pEopiCB -- Auto-select's Q1/Q2/Q3 focus is time-dependent.

Default (>= AT_RISK_WINDOW_S (5:00) left in the current qualifying part, or the
remaining-time signal is not yet known): every push-lap driver, closest to finishing
their timed lap first (by track %).

Inside AT_RISK_WINDOW_S:
  - Q1/Q2: narrows to at-risk-on-push (by track %) then the rest-on-push (by track %),
    as before this card.
  - Q3: narrows to {predicted top-5 ∪ current top-5} by track %, then all others, as
    before this card.

Time-remaining source: `ExtrapolatedClock.Remaining` (HH:MM:SS), scoped per quali part
(implementation-plan.md: verified against capture data to reset at each part boundary).

Tie-break (open point, resolved as a judgment call -- see the processor's module
docstring): drivers tied on track % are ordered by predictedPos ascending, missing
predictions sorting last.
"""
import unittest
from datetime import datetime

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.dashboard_autoselect_processor import (
    AT_RISK_WINDOW_S,
    DashboardAutoSelectProcessor,
    _remaining_s,
)

CT = datetime(2026, 1, 1, 12, 0, 0)


def _clock(remaining: str) -> dict:
    return {"Utc": "2026-01-01T12:00:00Z", "Remaining": remaining, "Extrapolating": True}


class _Base(unittest.TestCase):
    def setUp(self):
        self.bus = SessionMessageBus()
        self.proc = DashboardAutoSelectProcessor(self.bus, "qualifying")
        self.proc.subscribe()
        self.picks = []
        self.bus.on("dashAutoSelect", lambda d, ct: self.picks.append(list(d)))

    def _start(self, part: int):
        self.bus.emit("qualifyingPart", part, CT)
        self.bus.emit("sessionInfo", {"sessionStatus": "Started"}, CT)


class Q12DefaultFocusTest(_Base):
    """Default ordering (>=5:00 left, or unknown): every push-lap driver, closest to
    finishing first -- NOT narrowed to the at-risk zone."""

    def test_defaults_to_push_by_track_pct_when_plenty_of_time_left(self):
        self._start(1)
        # Driver "1" is at-risk (P18, in Q1's P13-20 drop zone) but far from finishing;
        # driver "44" is safely placed (P3) but much closer to finishing their lap.
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 18}, {"num": "44", "position": 3},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 20.0], "44": [0, 0, 80.0]}, CT)
        for n in ("1", "44"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit("ExtrapolatedClock", _clock("00:10:00"), CT)   # 10:00 left, well outside the window

        self.assertEqual(self.proc._pick_q12(1), ["44", "1"],
                          "default ordering must be by track %% alone, not the at-risk zone")

    def test_defaults_to_push_by_track_pct_when_remaining_time_unknown(self):
        self._start(1)
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 18}, {"num": "44", "position": 3},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 20.0], "44": [0, 0, 80.0]}, CT)
        for n in ("1", "44"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        # No ExtrapolatedClock emitted at all -- remaining time unknown.
        self.assertEqual(self.proc._pick_q12(1), ["44", "1"])


class Q12AtRiskWindowTest(_Base):
    """Inside AT_RISK_WINDOW_S: narrows to at-risk-zone-first, as before this card."""

    def test_narrows_to_at_risk_zone_inside_five_minutes(self):
        self._start(1)
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 18}, {"num": "44", "position": 3},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 20.0], "44": [0, 0, 80.0]}, CT)
        for n in ("1", "44"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit("ExtrapolatedClock", _clock("00:04:59"), CT)   # inside the window

        self.assertEqual(self.proc._pick_q12(1), ["1", "44"],
                          "inside the at-risk window, the at-risk (P18, drop zone) driver leads")

    def test_boundary_is_strictly_less_than_window(self):
        self._start(1)
        self.bus.emit("standings", {"drivers": [{"num": "1", "position": 18}]}, CT)
        self.bus.emit("position", {"1": [0, 0, 20.0]}, CT)
        self.bus.emit(f"driverLapClassification:1", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit("ExtrapolatedClock", _clock("00:05:00"), CT)   # exactly AT_RISK_WINDOW_S -- NOT yet inside
        self.assertFalse(self.proc._at_risk_window())
        self.bus.emit("ExtrapolatedClock", _clock("00:04:59"), CT)
        self.assertTrue(self.proc._at_risk_window())


class Q3TimeGatedFocusTest(_Base):
    """Q3: default = push-by-track-%% (no top5/predicted narrowing); inside 5:00,
    narrows to {predicted top-5 ∪ current top-5}, as before this card."""

    def test_q3_default_is_push_by_track_pct_not_top5_narrowed(self):
        self._start(3)
        # Driver "20" is P8 (outside top 5) but closest to finishing; "1" is P1 (top 5)
        # but far from finishing. Pre-card behavior (top5-narrowed) would always put
        # the P1 driver first; the new default must not.
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 1}, {"num": "20", "position": 8},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 10.0], "20": [0, 0, 90.0]}, CT)
        for n in ("1", "20"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit("ExtrapolatedClock", _clock("00:08:00"), CT)

        self.assertEqual(self.proc._pick_q3(), ["20", "1"])

    def test_q3_narrows_to_predicted_top5_inside_window(self):
        self._start(3)
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 1}, {"num": "20", "position": 8},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 10.0], "20": [0, 0, 90.0]}, CT)
        for n in ("1", "20"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit("ExtrapolatedClock", _clock("00:04:00"), CT)

        self.assertEqual(self.proc._pick_q3(), ["1", "20"],
                          "inside the window, current-top-5 driver 1 leads over the "
                          "faster-approaching but P8 driver 20")


class TieBreakTest(_Base):
    """Judgment call: drivers tied on track %% are ordered by predictedPos ascending."""

    def test_tied_track_pct_broken_by_better_predicted_position(self):
        self._start(1)
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 10}, {"num": "2", "position": 11},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 50.0], "2": [0, 0, 50.0]}, CT)
        for n in ("1", "2"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit(f"lapPrediction:1", {"predictedPos": 4}, CT)
        self.bus.emit(f"lapPrediction:2", {"predictedPos": 2}, CT)

        self.assertEqual(self.proc._push_by_dp(), ["2", "1"])

    def test_tied_track_pct_missing_prediction_sorts_last(self):
        self._start(1)
        self.bus.emit("standings", {"drivers": [
            {"num": "1", "position": 10}, {"num": "2", "position": 11},
        ]}, CT)
        self.bus.emit("position", {"1": [0, 0, 50.0], "2": [0, 0, 50.0]}, CT)
        for n in ("1", "2"):
            self.bus.emit(f"driverLapClassification:{n}", {"lap": 1, "trackPct": 0.0, "type": "PUSH"}, CT)
        self.bus.emit(f"lapPrediction:2", {"predictedPos": 2}, CT)   # driver 1 has none

        self.assertEqual(self.proc._push_by_dp(), ["2", "1"])


class RemainingSecondsParserTest(unittest.TestCase):
    """Direct unit coverage of `_remaining_s` -- the ExtrapolatedClock.Remaining parser."""

    def test_hhmmss(self):
        self.assertEqual(_remaining_s("00:04:30"), 270.0)

    def test_mmss(self):
        self.assertEqual(_remaining_s("04:30"), 270.0)

    def test_non_string_input(self):
        self.assertIsNone(_remaining_s(None))
        self.assertIsNone(_remaining_s(270))

    def test_no_colon(self):
        self.assertIsNone(_remaining_s("270"))

    def test_malformed_component_raises_value_error_internally(self):
        self.assertIsNone(_remaining_s("00:0x:00"))

    def test_unexpected_component_count_falls_through(self):
        self.assertIsNone(_remaining_s("1:2:3:4"))


if __name__ == "__main__":
    unittest.main()
