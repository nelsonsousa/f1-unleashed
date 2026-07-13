"""Reprocess all cached sessions for one season, in chronological order.

Rebuilds each session's transient processed DB from `live.jsonl` with the CURRENT
processor code (`force=True` deletes the stale scratch DB first) and runs the
finalize chain — pecking-order prediction, pit-lane transit estimate, and any
other post-session analysis wired into the preprocessor. Sessions are processed
in session-key order within each event (the key prefix is chronological), so the
event-scoped analysis accumulation (FP1 -> FP2 -> FP3 -> Q) sees each prior
session's output before the next is computed.

The processed DB is a scratch file under DATA_DIR/tmp; the durable outputs are the
analysis JSONs under DATA_DIR/analysis. Run offline (stop the server first if a
client might be replaying a session being rebuilt).

Run:
    PYTHONPATH=. python -m utils.scripts.reprocess_year --year 2026
    PYTHONPATH=. python -m utils.scripts.reprocess_year --year 2026 --event Barcelona
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root, for `app.*` imports

from app.config import CACHE_DIR                                # noqa: E402
from app.processing.preprocessor import SessionPreProcessor     # noqa: E402
from app.services.cache_manager import cache_manager            # noqa: E402

cache_manager.cache_dir = CACHE_DIR


def _session_key(session_dir: Path) -> int:
    """Numeric session-key prefix (e.g. '11307_Race' -> 11307). Chronological within an event."""
    head = session_dir.name.split("_", 1)[0]
    return int(head) if head.isdigit() else 0


def event_sessions(event_dir: Path) -> list[Path]:
    return sorted(
        (s for s in event_dir.iterdir() if s.is_dir() and (s / "live.jsonl").exists()),
        key=_session_key,
    )


async def reprocess_session(session_dir: Path) -> None:
    pre = SessionPreProcessor(session_dir, "")   # session_type auto-derived from SessionInfo
    try:
        await pre.run(force=True)
    finally:
        pre.close()


async def reprocess_year(year: int, event_filter: Optional[str] = None) -> tuple[int, int]:
    root = CACHE_DIR / str(year)
    if not root.exists():
        print(f"No cache for {year} at {root}")
        return 0, 0
    events = sorted(e for e in root.iterdir() if e.is_dir())
    if event_filter:
        events = [e for e in events if event_filter.lower() in e.name.lower()]
    n_ok = n_fail = 0
    for ev in events:
        sessions = event_sessions(ev)
        print(f"\n=== {ev.name} ({len(sessions)} sessions) ===", flush=True)
        for sp in sessions:
            t0 = time.perf_counter()
            try:
                await reprocess_session(sp)
                print(f"  ok   {sp.name}  ({time.perf_counter() - t0:.1f}s)", flush=True)
                n_ok += 1
            except Exception as e:                                # noqa: BLE001 - report + continue
                print(f"  FAIL {sp.name}  {type(e).__name__}: {e}", flush=True)
                n_fail += 1
    print(f"\n{year}: {n_ok} reprocessed, {n_fail} failed.", flush=True)
    return n_ok, n_fail


def main() -> None:
    ap = argparse.ArgumentParser(description="Reprocess a season's cached sessions.")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--event", default=None, help="substring filter, e.g. Barcelona")
    args = ap.parse_args()
    asyncio.run(reprocess_year(args.year, args.event))


if __name__ == "__main__":
    main()
