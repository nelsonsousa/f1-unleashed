"""WB-9 batch 2 — FiaStewardsProcessor: race/sprint only. Maintains an
internal stack of penalty/investigation/flag indicators driven by
RaceControlMessages text-parsing and re-issues each affected driver's
current indicator list on `driverPenalties:{num}` (see module docstring
for the full kind table and state-machine rules).
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.fia_stewards_processor import FiaStewardsProcessor


class FiaStewardsProcessorTests(unittest.TestCase):
    def _make(self, session_type="race"):
        bus = SessionMessageBus()
        proc = FiaStewardsProcessor(bus, session_type)
        proc.subscribe()
        emits = {}  # num -> list of emitted indicator lists, in order
        bus.on("driverPenalties:44", lambda d, ct: emits.setdefault("44", []).append(d))
        bus.on("driverPenalties:1", lambda d, ct: emits.setdefault("1", []).append(d))
        return bus, proc, emits

    def _ct(self, sec=0):
        return datetime(2026, 7, 18, 10, 0, sec, tzinfo=timezone.utc)

    def _rcm(self, bus, msg, idx="0", ct=None):
        bus.emit("RaceControlMessages", {"Messages": {idx: msg}}, ct or self._ct())

    def test_inactive_outside_race_and_sprint(self):
        bus, proc, emits = self._make(session_type="Qualifying")
        self._rcm(bus, {"Category": "Other",
                         "Message": "CAR 44 (HAM) UNDER INVESTIGATION"})
        self.assertEqual(emits, {})

    def test_case_insensitive_session_type_still_active(self):
        bus, proc, emits = self._make(session_type="RACE")
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - CAUSING A COLLISION"})
        self.assertIn("44", emits)

    def test_track_limits_flag_pushes_indicator_and_dedups(self):
        bus, proc, emits = self._make()
        msg = {"Category": "Flag", "Flag": "BLACK AND WHITE",
               "Message": "BLACK AND WHITE FLAG CAR 44 (HAM) - TRACK LIMITS"}
        self._rcm(bus, msg, idx="0")
        self.assertEqual(len(emits["44"][-1]), 1)
        self.assertEqual(emits["44"][-1][0]["kind"], "trackLimits")

        # A second identical warning for the same driver must not duplicate.
        self._rcm(bus, msg, idx="1")
        self.assertEqual(len(emits["44"]), 1, "no re-emit when already present")

    def test_blue_flag_pushed_then_refreshed_not_duplicated(self):
        bus, proc, emits = self._make()
        bus.emit("SessionInfo", {}, self._ct(0))
        self._rcm(bus, {"Category": "Flag", "Flag": "BLUE",
                         "Message": "WAVED BLUE FLAG FOR CAR 44 (HAM)"},
                   idx="0", ct=self._ct(0))
        self.assertEqual(len(emits["44"][-1]), 1)
        self.assertEqual(emits["44"][-1][0]["kind"], "blueFlag")

        # Second blue flag for the same driver refreshes the timer in place
        # (still exactly one indicator).
        self._rcm(bus, {"Category": "Flag", "Flag": "BLUE",
                         "Message": "WAVED BLUE FLAG FOR CAR 44 (HAM)"},
                   idx="1", ct=self._ct(2))
        self.assertEqual(len(emits["44"][-1]), 1)

    def test_blue_flag_expires_after_10s_on_clock_tick(self):
        bus, proc, emits = self._make()
        bus.emit("SessionInfo", {}, self._ct(0))
        self._rcm(bus, {"Category": "Flag", "Flag": "BLUE",
                         "Message": "WAVED BLUE FLAG FOR CAR 44 (HAM)"},
                   idx="0", ct=self._ct(0))
        self.assertEqual(len(emits["44"][-1]), 1)

        # Before expiry: a clock tick must not clear it.
        bus.emit("clock", {}, self._ct(5))
        self.assertEqual(len(emits["44"]), 1)

        # After expiry (>10s later): clock tick clears it.
        bus.emit("clock", {}, self._ct(11))
        self.assertEqual(emits["44"][-1], [])

    def test_clock_tick_before_session_start_known_is_a_noop(self):
        bus, proc, emits = self._make()
        # No SessionInfo yet, so _start_time is None -> _session_ms is None.
        bus.emit("clock", {}, self._ct(5))
        self.assertEqual(emits, {})

    def test_under_investigation_pushed_and_deduped(self):
        bus, proc, emits = self._make()
        msg = {"Category": "Other",
               "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - CAUSING A COLLISION"}
        self._rcm(bus, msg, idx="0")
        self.assertEqual(len(emits["44"][-1]), 1)
        self.assertEqual(emits["44"][-1][0]["kind"], "investigation")
        self.assertEqual(emits["44"][-1][0]["color"], "yellow")

        self._rcm(bus, msg, idx="1")
        self.assertEqual(len(emits["44"]), 1, "identical incident must not duplicate")

    def test_no_further_action_clears_investigation(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - CAUSING A COLLISION"},
                   idx="0")
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) NO FURTHER ACTION - CAUSING A COLLISION"},
                   idx="1")
        self.assertEqual(emits["44"][-1], [])

    def test_will_be_investigated_after_drops_indicator_entirely(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - TRACK LIMITS"},
                   idx="0")
        self.assertEqual(len(emits["44"][-1]), 1)
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) WILL BE INVESTIGATED AFTER THE RACE - TRACK LIMITS"},
                   idx="1")
        self.assertEqual(emits["44"][-1], [], "deferred reviews are dropped, not shown")

    def test_noted_supersedes_investigation(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - TRACK LIMITS"},
                   idx="0")
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) NOTED - TRACK LIMITS"},
                   idx="1")
        cur = emits["44"][-1]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["kind"], "noted")
        self.assertEqual(cur[0]["color"], "white")

    def test_time_penalty_awarded_resolves_investigation_and_shows_orange(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - CAUSING A COLLISION"},
                   idx="0")
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) 5 SECOND TIME PENALTY - CAUSING A COLLISION"},
                   idx="1")
        cur = emits["44"][-1]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["kind"], "5s")
        self.assertEqual(cur[0]["label"], "+5s")
        self.assertEqual(cur[0]["color"], "orange")

    def test_drive_through_and_stop_go_penalties_recognised(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) DRIVE THROUGH PENALTY - CAUSING A COLLISION"},
                   idx="0")
        self.assertEqual(emits["44"][-1][0]["kind"], "dt")

        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 1 (VER) STOP AND GO PENALTY - UNSAFE RELEASE"},
                   idx="1")
        self.assertEqual(emits["1"][-1][0]["kind"], "sg")

    def test_penalty_served_clears_matching_kind_by_car(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) 5 SECOND TIME PENALTY - CAUSING A COLLISION"},
                   idx="0")
        self.assertEqual(len(emits["44"][-1]), 1)
        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) 5 SECOND PENALTY SERVED"},
                   idx="1")
        self.assertEqual(emits["44"][-1], [])

    def test_track_limits_penalty_clears_the_track_limits_flag(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Flag", "Flag": "BLACK AND WHITE",
                         "Message": "BLACK AND WHITE FLAG CAR 44 (HAM) - TRACK LIMITS"},
                   idx="0")
        self.assertEqual(emits["44"][-1][0]["kind"], "trackLimits")

        self._rcm(bus, {"Category": "Other",
                         "Message": "FIA STEWARDS: CAR 44 (HAM) 5 SECOND TIME PENALTY - TRACK LIMITS"},
                   idx="1")
        cur = emits["44"][-1]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["kind"], "5s", "track-limits flag replaced by the penalty")

    def test_non_fia_stewards_other_message_is_ignored(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Other", "Message": "GREEN LIGHT - TRACK CLEAR"})
        self.assertEqual(emits, {})

    def test_flag_category_without_car_number_is_ignored(self):
        bus, proc, emits = self._make()
        self._rcm(bus, {"Category": "Flag", "Flag": "BLUE", "Message": "WAVED BLUE FLAG"})
        self.assertEqual(emits, {})

    def test_non_dict_payload_is_ignored(self):
        bus, proc, emits = self._make()
        bus.emit("RaceControlMessages", "oops", self._ct())
        self.assertEqual(emits, {})

    def test_rcm_index_dedup_skips_already_seen(self):
        bus, proc, emits = self._make()
        msg = {"Category": "Other",
               "Message": "FIA STEWARDS: CAR 44 (HAM) UNDER INVESTIGATION - TRACK LIMITS"}
        self._rcm(bus, msg, idx="0")
        first_len = len(emits["44"])
        # Re-send the same index -> must be skipped entirely (no re-processing).
        bus.emit("RaceControlMessages", {"Messages": {"0": msg}}, self._ct())
        self.assertEqual(len(emits["44"]), first_len)


if __name__ == "__main__":
    unittest.main()
