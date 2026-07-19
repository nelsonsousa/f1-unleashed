"""B02 [P3] — SessionEngine._scan_time_bounds must not crash on an empty or
degenerate live.jsonl. Reproduces the NameError (the `line` variable is unbound
when the file yields no lines) and pins what "right" is: no exception, and the
session bounds simply left unset.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.processing.session import SessionEngine


def _engine(session_path: Path) -> SessionEngine:
    e = SessionEngine.__new__(SessionEngine)   # bypass __init__ (asyncio task setup)
    e._session_path = Path(session_path)
    e._start_time = None
    e._clock = None
    e._duration = 0.0
    return e


class ScanTimeBounds(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    async def test_empty_file_leaves_bounds_unset_without_crashing(self):
        # 0 lines — the reported NameError case
        (self.dir / "live.jsonl").write_text("")
        e = _engine(self.dir)
        await e._scan_time_bounds()            # RIGHT: no exception
        self.assertIsNone(e._start_time)       # RIGHT: bounds left unset
        self.assertIsNone(e._clock)

    async def test_blank_lines_only_leave_bounds_unset(self):
        (self.dir / "live.jsonl").write_text("\n   \n\n")
        e = _engine(self.dir)
        await e._scan_time_bounds()
        self.assertIsNone(e._start_time)

    async def test_valid_timestamps_set_the_bounds(self):
        rows = [
            {"Type": "a", "DateTime": "2026-07-18T10:00:00.000Z"},
            {"Type": "b", "DateTime": "2026-07-18T10:05:00.000Z"},
        ]
        (self.dir / "live.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
        e = _engine(self.dir)
        await e._scan_time_bounds()
        self.assertIsNotNone(e._start_time)    # RIGHT: first ts becomes the start
        self.assertIsNotNone(e._clock)
        self.assertAlmostEqual(e._duration, 300.0, places=1)   # 5 min between the two


if __name__ == "__main__":
    unittest.main()
