"""Reprocess every cached season (all years).

Thin wrapper over reprocess_year: discovers each year directory under the cache
root and reprocesses it in order. See reprocess_year for what "reprocess" does.

Run:
    PYTHONPATH=. python -m utils.scripts.reprocess_all
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root, for `app.*` imports

from app.config import CACHE_DIR                                # noqa: E402
from utils.scripts.reprocess_year import reprocess_year         # noqa: E402


async def reprocess_all() -> None:
    years = sorted(int(d.name) for d in CACHE_DIR.iterdir() if d.is_dir() and d.name.isdigit())
    if not years:
        print(f"No season directories under {CACHE_DIR}")
        return
    total_ok = total_fail = 0
    for y in years:
        print(f"\n########## {y} ##########", flush=True)
        ok, fail = await reprocess_year(y)
        total_ok += ok
        total_fail += fail
    print(f"\nAll seasons: {total_ok} reprocessed, {total_fail} failed.", flush=True)


if __name__ == "__main__":
    asyncio.run(reprocess_all())
