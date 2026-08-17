"""Direct unit coverage for `file_reader.parse_scheduled_start_utc`
(2026-08-17-047 WB-1 resume, file-impact-map.md §1.5) -- the single shared
`StartDate + GmtOffset -> UTC` helper used at every `SessionPreProcessor`
construction site. Covers the branches the integration-level tests
(preprocessor/live-capture) only exercise incidentally via their own happy
paths: non-dict input, missing fields, and unparseable fields.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.file_reader import parse_scheduled_start_utc


class ParseScheduledStartUtc(unittest.TestCase):
    def test_positive_offset_subtracts_correctly(self):
        result = parse_scheduled_start_utc(
            {"StartDate": "2026-07-04T16:00:00", "GmtOffset": "01:00:00"})
        self.assertEqual(result, datetime(2026, 7, 4, 15, 0, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(result.tzinfo, "result must be tz-aware")

    def test_negative_offset_adds_correctly(self):
        result = parse_scheduled_start_utc(
            {"StartDate": "2026-07-04T16:00:00", "GmtOffset": "-05:00:00"})
        self.assertEqual(result, datetime(2026, 7, 4, 21, 0, 0, tzinfo=timezone.utc))

    def test_offset_with_seconds_component(self):
        result = parse_scheduled_start_utc(
            {"StartDate": "2026-07-04T16:00:00", "GmtOffset": "01:30:00"})
        self.assertEqual(result, datetime(2026, 7, 4, 14, 30, 0, tzinfo=timezone.utc))

    def test_non_dict_input_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc(None))
        self.assertIsNone(parse_scheduled_start_utc([]))
        self.assertIsNone(parse_scheduled_start_utc("not a dict"))

    def test_missing_start_date_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc({"GmtOffset": "01:00:00"}))

    def test_missing_gmt_offset_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc({"StartDate": "2026-07-04T16:00:00"}))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc({}))

    def test_unparseable_start_date_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc(
            {"StartDate": "not-a-date", "GmtOffset": "01:00:00"}))

    def test_unparseable_gmt_offset_returns_none(self):
        self.assertIsNone(parse_scheduled_start_utc(
            {"StartDate": "2026-07-04T16:00:00", "GmtOffset": "not-an-offset"}))

    def test_result_matches_the_shared_helper_used_by_live_capture(self):
        """Confirms `live_capture.LiveCaptureService._scheduled_start_utc`
        (which now delegates here, 2026-08-17-047) produces the same tz-aware
        result this direct call does -- proving there is no longer a
        naive/aware discrepancy between the two."""
        from app.services.live_capture import LiveCaptureService
        data = {"StartDate": "2026-07-04T16:00:00", "GmtOffset": "01:00:00"}
        direct = parse_scheduled_start_utc(data)
        via_live_capture = LiveCaptureService._scheduled_start_utc(data)
        self.assertEqual(direct, via_live_capture)
        self.assertIsNotNone(via_live_capture.tzinfo)


if __name__ == "__main__":
    unittest.main()
