"""B07 (2FPsLcpN): the reorder-flush window (newest_ts) must advance on NON-.z
messages too, or a no-telemetry stretch stalls the live edge then bursts. The
timing/stall itself is a live-tail behaviour (replay always drains at EOF, so it
isn't unit-reproducible without the live harness); these tests guard that the
reorder path delivers non-.z streams completely and in timestamp order — i.e.
the fix doesn't break delivery/ordering.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.file_reader import read_jsonl


def _session(rows):
    d = Path(tempfile.mkdtemp())
    (d / "live.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


class ReorderDelivery(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, d, **kw):
        return [m async for m in read_jsonl(d, **kw)]

    async def test_non_z_only_stream_delivered_in_order(self):
        # The B07 scenario: no CarData/Position.z at all, spanning several seconds.
        rows = [
            {"Type": "TrackStatus", "DateTime": "2026-07-18T10:00:00.000Z", "Json": {"s": 1}},
            {"Type": "RaceControlMessages", "DateTime": "2026-07-18T10:00:03.000Z", "Json": {"m": "a"}},
            {"Type": "Heartbeat", "DateTime": "2026-07-18T10:00:05.000Z", "Json": {}},
        ]
        msgs = await self._drain(_session(rows))
        self.assertEqual([m.topic for m in msgs],
                         ["TrackStatus", "RaceControlMessages", "Heartbeat"])
        ts = [m.timestamp for m in msgs]
        self.assertEqual(ts, sorted(ts))          # delivered in timestamp order

    async def test_out_of_order_within_window_is_sorted(self):
        rows = [
            {"Type": "A", "DateTime": "2026-07-18T10:00:00.500Z", "Json": {}},
            {"Type": "B", "DateTime": "2026-07-18T10:00:00.100Z", "Json": {}},   # earlier ts, later in file
            {"Type": "C", "DateTime": "2026-07-18T10:00:00.300Z", "Json": {}},
        ]
        msgs = await self._drain(_session(rows))
        self.assertEqual([m.topic for m in msgs], ["B", "C", "A"])   # reordered by ts


if __name__ == "__main__":
    unittest.main()
