"""Unit coverage for SessionPreProcessor._filter_message operating on
NormalizedMessage (post 2026-07-27-003 rewiring) — the post-gate 1h cutoff
and the RCM/SessionData stale-entry stripping, both now keyed on
`utc_timestamp` rather than the raw envelope (architecture-plan.md §A.7.7).
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor
from app.processing.stream_normalizer import NormalizedMessage


def _msg(topic, data, utc_ts) -> NormalizedMessage:
    return NormalizedMessage(topic=topic, data=data, envelope_ts=utc_ts,
                              utc_timestamp=utc_ts, offset_ms=0)


class FilterMessageCutoff(unittest.TestCase):
    def _proc(self) -> SessionPreProcessor:
        p = SessionPreProcessor.__new__(SessionPreProcessor)
        p._cutoff = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)
        return p

    def test_message_before_cutoff_is_dropped(self):
        p = self._proc()
        msg = _msg("Heartbeat", {}, p._cutoff - timedelta(seconds=1))
        self.assertIsNone(p._filter_message(msg))

    def test_message_after_cutoff_survives(self):
        p = self._proc()
        msg = _msg("Heartbeat", {}, p._cutoff + timedelta(seconds=1))
        self.assertIs(p._filter_message(msg), msg)

    def test_no_cutoff_set_returns_message_unchanged(self):
        p = SessionPreProcessor.__new__(SessionPreProcessor)
        p._cutoff = None
        msg = _msg("Heartbeat", {}, datetime.now(timezone.utc))
        self.assertIs(p._filter_message(msg), msg)

    def test_rcm_stale_entries_stripped_after_cutoff_check(self):
        p = self._proc()
        stale_utc = "2026-07-18T08:00:00.000Z"   # before cutoff (09:00)
        fresh_utc = "2026-07-18T09:30:00.000Z"    # after cutoff
        data = {"Messages": {
            "0": {"Message": "OLD", "Utc": stale_utc},
            "1": {"Message": "NEW", "Utc": fresh_utc},
        }}
        msg = _msg("RaceControlMessages", data, p._cutoff + timedelta(minutes=5))
        filtered = p._filter_message(msg)
        self.assertIsNotNone(filtered)
        self.assertEqual(filtered.data, {"Messages": {"1": {"Message": "NEW", "Utc": fresh_utc}}})
        # offset_ms/envelope_ts/utc_timestamp are carried through unchanged.
        self.assertEqual(filtered.utc_timestamp, msg.utc_timestamp)
        self.assertEqual(filtered.offset_ms, msg.offset_ms)

    def test_rcm_message_with_all_entries_stale_is_dropped_entirely(self):
        p = self._proc()
        data = {"Messages": {"0": {"Message": "OLD", "Utc": "2026-07-18T08:00:00.000Z"}}}
        msg = _msg("RaceControlMessages", data, p._cutoff + timedelta(minutes=5))
        self.assertIsNone(p._filter_message(msg))

    def test_tz_naive_utc_timestamp_is_treated_as_utc_and_survives(self):
        """Coverage for the tz-naive branch of `_filter_message` — a
        `NormalizedMessage.utc_timestamp` with no tzinfo must be treated as
        UTC (not raise on the naive/aware comparison against `self._cutoff`,
        which is always tz-aware)."""
        p = self._proc()
        naive_ts = (p._cutoff + timedelta(seconds=1)).replace(tzinfo=None)
        msg = _msg("Heartbeat", {}, naive_ts)
        filtered = p._filter_message(msg)
        self.assertIsNotNone(filtered)
        self.assertIs(filtered, msg)

    def test_tz_naive_utc_timestamp_before_cutoff_is_dropped(self):
        p = self._proc()
        naive_ts = (p._cutoff - timedelta(seconds=1)).replace(tzinfo=None)
        msg = _msg("Heartbeat", {}, naive_ts)
        self.assertIsNone(p._filter_message(msg))


if __name__ == "__main__":
    unittest.main()
