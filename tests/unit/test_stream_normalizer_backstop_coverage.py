"""Coverage-closing tests for `StreamNormalizer.poll_wall_clock_backstop`
(AC-3, requirement-spec.md) branches not exercised by
`tests/unit/test_stream_normalizer_reorder_buffer.py`'s single-entry
assumed-interface test: the empty-buffer/disabled-backstop early return, and
an entry that is NOT yet due (kept in the buffer) alongside one that is.
"""
import unittest
from datetime import datetime, timezone

from app.processing.file_reader import RawLine
from app.processing.stream_normalizer import StreamNormalizer

import base64
import json
import zlib


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _line(topic: str, data, envelope_ts: str) -> RawLine:
    return RawLine(topic=topic, data=data, envelope_ts=_dt(envelope_ts))


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


def _car_data_z(ts: str, num: str = "1", speed: int = 200) -> str:
    return _compress({"Entries": [{"Utc": ts, "Cars": {num: {"Channels": {"2": speed}}}}]})


class BackstopDisabledOrEmpty(unittest.TestCase):
    def test_backstop_disabled_returns_empty(self):
        norm = StreamNormalizer(wall_clock_backstop_s=None)
        norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:00.000Z"), "2026-07-18T10:00:00.100Z"))
        self.assertEqual(norm.poll_wall_clock_backstop(), [])

    def test_empty_buffer_returns_empty(self):
        fake_now = [0.0]
        norm = StreamNormalizer(wall_clock_backstop_s=10.0, _now=lambda: fake_now[0])
        self.assertEqual(norm.poll_wall_clock_backstop(), [])


class MixedDueAndNotDueEntries(unittest.TestCase):
    def test_only_the_overdue_entry_releases_the_other_stays_buffered(self):
        fake_now = [0.0]

        def _now():
            return fake_now[0]

        norm = StreamNormalizer(wall_clock_backstop_s=10.0, _now=_now)
        # Entry 1 inserted at wall time 0.
        norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:00.000Z"), "2026-07-18T10:00:00.100Z"))
        # Advance wall time; entry 2 inserted at wall time 5 -- not yet
        # overdue when the poll below happens at wall time 11.
        fake_now[0] = 5.0
        norm.process_line(_line(
            "Position.z", _compress({"Position": [{"Timestamp": "2026-07-18T10:00:00.050Z",
                                                     "Entries": {"1": [0, 0, 5.0]}}]}),
            "2026-07-18T10:00:00.150Z"))

        fake_now[0] = 11.0   # entry 1 (age 11s) overdue; entry 2 (age 6s) not
        released = norm.poll_wall_clock_backstop()
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].topic, "CarData.z")

        # entry 2 remains held -- a second poll once it too becomes overdue
        # releases it.
        fake_now[0] = 20.0
        released2 = norm.poll_wall_clock_backstop()
        self.assertEqual(len(released2), 1)
        self.assertEqual(released2[0].topic, "Position.z")


if __name__ == "__main__":
    unittest.main()
