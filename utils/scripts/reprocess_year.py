"""Reprocess all sessions for a given year (rebuilds session.db)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.processing.preprocessor import SessionPreProcessor


def _infer_type(name: str) -> str:
    n = name.lower()
    parts = n.split("_", 1)
    if parts[0].isdigit() and len(parts) > 1:
        n = parts[1]
    if "qualifying" in n or "shootout" in n:
        return "qualifying"
    if n in ("race", "sprint"):
        return "race"
    return "practice"


async def reprocess(session_path: Path) -> None:
    print(f"→ {session_path}")
    pp = SessionPreProcessor(session_path, _infer_type(session_path.name))
    try:
        await pp.run(tail_follow=False, force=True)
    finally:
        pp.close()


async def main(year: str) -> None:
    root = Path("data/livetiming_cache") / year
    sessions = sorted(p.parent for p in root.glob("*/*/live.jsonl"))
    print(f"Reprocessing {len(sessions)} sessions for {year}")
    for sp in sessions:
        await reprocess(sp)
    print("Done.")


if __name__ == "__main__":
    year = sys.argv[1] if len(sys.argv) > 1 else "2026"
    asyncio.run(main(year))
