"""Live-simulation pacer (card 3g9U4kQz / VOPkIiAh piece 4): read_jsonl(pace=True)
releases each line at its envelope timestamp (relative to the first), scaled by
`speed`, so a completed live.jsonl replays with the real arrival cadence. Uses an
injected clock + sleep so the test is deterministic and instant.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.file_reader import read_jsonl


class FakeTime:
    """Monotonic clock whose sleep() advances it — so pacing math accumulates."""
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(round(d, 6))
        self.t += d


def _session(rows):
    d = Path(tempfile.mkdtemp())
    (d / "live.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


ROWS = [
    {"Type": "a", "DateTime": "2026-07-18T10:00:00.000Z", "Json": {}},
    {"Type": "b", "DateTime": "2026-07-18T10:00:01.000Z", "Json": {}},
    {"Type": "c", "DateTime": "2026-07-18T10:00:03.000Z", "Json": {}},
]


class LiveSimPacer(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, d, **kw):
        return [m async for m in read_jsonl(d, **kw)]

    async def test_paces_reads_by_envelope_timestamp(self):
        ft = FakeTime()
        msgs = await self._drain(_session(ROWS), pace=True, speed=1.0, _now=ft.now, _sleep=ft.sleep)
        self.assertEqual(len(msgs), 3)                 # all lines still delivered
        self.assertEqual(ft.sleeps, [1.0, 2.0])        # gaps 0→1s and 1→3s

    async def test_speed_multiplier_scales_delays(self):
        ft = FakeTime()
        await self._drain(_session(ROWS), pace=True, speed=2.0, _now=ft.now, _sleep=ft.sleep)
        self.assertEqual(ft.sleeps, [0.5, 1.0])        # 2x → half the wall time

    async def test_no_pace_means_no_sleeps(self):
        ft = FakeTime()
        msgs = await self._drain(_session(ROWS), pace=False, _now=ft.now, _sleep=ft.sleep)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(ft.sleeps, [])                # fast path: no pacing


if __name__ == "__main__":
    unittest.main()
