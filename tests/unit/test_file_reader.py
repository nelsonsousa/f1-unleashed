"""`file_reader.py` — the pure tail-follow reader (2026-07-27-003 pipeline
redesign): bytes -> lines -> RawLine, in strict file order, no reordering, no
buffering window, no `.z` handling.

AC-3 (requirement-spec.md §9): a non-`.z`-only stream is delivered completely
and in file order. Pre-redesign this was covered by
`tests/test_reorder_flush.py::test_non_z_only_stream_delivered_in_order`
against the old reorder-buffer reader; that file was retired wholesale when
the reorder buffer itself was removed (DECISIONS.md, "Explicitly out of
scope"), but the file-order guarantee it proved is still a real, current
behaviour of the new reader and deserves its own coverage here rather than
being silently dropped along with the reorder-specific cases.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.file_reader import read_jsonl, RawLine


def _session(rows):
    d = Path(tempfile.mkdtemp())
    (d / "live.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


class FileOrderDelivery(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, d, **kw):
        return [m async for m in read_jsonl(d, **kw)]

    async def test_non_z_only_stream_delivered_completely_and_in_file_order(self):
        """The B07 scenario (originally guarded by the retired reorder
        buffer): no CarData/Position.z at all, spanning several seconds —
        every line must still be delivered, in exactly the order it appears
        in the file (the reader does no reordering of its own)."""
        rows = [
            {"Type": "TrackStatus", "DateTime": "2026-07-18T10:00:00.000Z", "Json": {"s": 1}},
            {"Type": "RaceControlMessages", "DateTime": "2026-07-18T10:00:03.000Z", "Json": {"m": "a"}},
            {"Type": "Heartbeat", "DateTime": "2026-07-18T10:00:05.000Z", "Json": {}},
        ]
        msgs = await self._drain(_session(rows))
        self.assertEqual([m.topic for m in msgs],
                          ["TrackStatus", "RaceControlMessages", "Heartbeat"])
        # File order is preserved even though it happens to already be
        # timestamp order here — the reader makes no timestamp-based
        # decision at all (AC-3: "no logic beyond sequential reading").
        self.assertEqual([m.envelope_ts.isoformat() for m in msgs],
                          sorted(m.envelope_ts.isoformat() for m in msgs))

    async def test_out_of_timestamp_order_lines_are_still_delivered_in_file_order(self):
        """Unlike the old reorder-buffer reader (which sorted within a
        window), the new pure tail-follow reader does NOT reorder — a line
        arriving out of timestamp order in the file is forwarded exactly as
        it appears, timestamp order or not (AC-3)."""
        rows = [
            {"Type": "A", "DateTime": "2026-07-18T10:00:00.500Z", "Json": {}},
            {"Type": "B", "DateTime": "2026-07-18T10:00:00.100Z", "Json": {}},  # earlier ts, later in file
            {"Type": "C", "DateTime": "2026-07-18T10:00:00.300Z", "Json": {}},
        ]
        msgs = await self._drain(_session(rows))
        self.assertEqual([m.topic for m in msgs], ["A", "B", "C"])  # file order, NOT ts order

    async def test_raw_line_fields_are_passed_through_unmodified(self):
        rows = [{"Type": "Heartbeat", "DateTime": "2026-07-18T10:00:00.000Z", "Json": {"n": 1}}]
        msgs = await self._drain(_session(rows))
        self.assertEqual(len(msgs), 1)
        msg = msgs[0]
        self.assertIsInstance(msg, RawLine)
        self.assertEqual(msg.topic, "Heartbeat")
        self.assertEqual(msg.data, {"n": 1})


if __name__ == "__main__":
    unittest.main()
