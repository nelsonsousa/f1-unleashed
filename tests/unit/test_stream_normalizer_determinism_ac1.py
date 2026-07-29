"""Determinism (requirement-spec.md §4; rules/data-processing.md; test-plan.md
§7's own note that this needs extending once the buffer lands): the AC-1
reorder buffer and AC-3's end-of-stream flush must stay byte-identical on
replay for their PRIMARY (content-driven) release path — extends
`tests/unit/test_stream_normalizer.py::CausalSinglePass::
test_replaying_same_lines_twice_is_byte_identical`'s coverage to actually
exercise `.z` topics, the reorder buffer, and `flush()`, none of which that
existing test touches (it only feeds ExtrapolatedClock/Heartbeat).

Also proves the wall-clock backstop's documented non-determinism exception
(class docstring, `poll_wall_clock_backstop`'s own docstring) is correctly
scoped: it never fires and never interferes with a normal, content-complete
replay (nothing to release out-of-band when the primary path already
covers everything via flush()).
"""
import base64
import json
import unittest
import zlib
from datetime import datetime, timedelta, timezone

from app.processing.file_reader import RawLine
from app.processing.stream_normalizer import StreamNormalizer


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


def _position_z(ts: str, num: str = "1", dp: float = 5.0) -> str:
    return _compress({"Position": [{"Timestamp": ts, "Entries": {num: [0, 0, dp]}}]})


def _build_lines() -> list[RawLine]:
    base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
    lines = []
    dp = 0.0
    for i in range(8):
        rnd = base + timedelta(seconds=i)
        car_ts = rnd + timedelta(milliseconds=900)
        pos_ts = rnd + timedelta(milliseconds=100)
        dp += 0.5
        lines.append(RawLine(topic="CarData.z", data=_car_data_z(car_ts.isoformat().replace("+00:00", "Z")),
                              envelope_ts=car_ts + timedelta(milliseconds=50)))
        lines.append(RawLine(topic="Position.z", data=_position_z(pos_ts.isoformat().replace("+00:00", "Z"), dp=dp),
                              envelope_ts=pos_ts + timedelta(milliseconds=50)))
        # A non-.z message interleaved -- AC-2's watermark-is-.z-only rule
        # means this must not perturb release timing/order either.
        lines.append(RawLine(topic="TrackStatus", data={"Status": "1"},
                              envelope_ts=rnd + timedelta(milliseconds=500)))
    return lines


class ReplayIsByteIdenticalThroughTheReorderBuffer(unittest.TestCase):
    def test_process_line_replay_including_z_topics_is_byte_identical(self):
        lines = _build_lines()

        def run():
            norm = StreamNormalizer()
            out = []
            for ln in lines:
                out.extend(norm.process_line(ln))
            return [(m.topic, m.utc_timestamp, m.offset_ms) for m in out]

        self.assertEqual(run(), run())

    def test_replay_including_the_end_of_stream_flush_is_byte_identical(self):
        """The primary path (process_line + flush at EOF) must be
        deterministic end to end -- this is the actual production sequence
        (preprocessor.py calls flush() once input is exhausted)."""
        lines = _build_lines()

        def run():
            norm = StreamNormalizer()
            out = []
            for ln in lines:
                out.extend(norm.process_line(ln))
            out.extend(norm.flush())
            return [(m.topic, m.utc_timestamp, m.offset_ms, m.data) for m in out]

        self.assertEqual(run(), run())

    def test_every_z_entry_is_released_exactly_once_across_process_and_flush(self):
        """Sanity check underpinning the determinism claim above: the
        combination of process_line's buffered releases plus the final
        flush() must account for every .z entry fed, exactly once (no
        entry silently lost to the buffer, none double-released)."""
        lines = _build_lines()
        norm = StreamNormalizer()
        out = []
        for ln in lines:
            out.extend(norm.process_line(ln))
        out.extend(norm.flush())
        z_msgs = [m for m in out if m.topic in ("CarData.z", "Position.z")]
        self.assertEqual(len(z_msgs), 16)   # 8 rounds * (1 CarData + 1 Position)
        timestamps = [m.utc_timestamp for m in z_msgs]
        self.assertEqual(len(timestamps), len(set(timestamps)))


class WallClockBackstopNeverFiresOnANormalCompleteReplay(unittest.TestCase):
    """The backstop's non-determinism exception (requirement-spec.md §4) is
    scoped to genuine whole-feed silence -- it must never fire, and
    therefore never interfere with the primary content-driven path, when a
    replay simply runs process_line + flush() to completion without ever
    polling it. Proven by confirming a non-polled backstop (present, but
    never invoked) has zero effect on the byte-identical result above --
    the ONLY way it could interfere is if something invoked
    `poll_wall_clock_backstop()`, which the primary replay path never does."""

    def test_backstop_configured_but_never_polled_does_not_change_output(self):
        lines = _build_lines()

        def run(backstop_s):
            norm = StreamNormalizer(wall_clock_backstop_s=backstop_s)
            out = []
            for ln in lines:
                out.extend(norm.process_line(ln))
            out.extend(norm.flush())
            return [(m.topic, m.utc_timestamp, m.offset_ms) for m in out]

        # Whether the backstop is disabled entirely or configured with some
        # duration, the primary (process_line + flush) path never calls
        # poll_wall_clock_backstop() itself, so the output must be identical
        # either way.
        self.assertEqual(run(None), run(180.0))


if __name__ == "__main__":
    unittest.main()
