"""Reprocess every session of a single event through SessionPreProcessor.

Run from the repo root:
    python -m utils.scripts.reprocess_event 2026 Melbourne
    python -m utils.scripts.reprocess_event 2026 Monte_Carlo --force

The event filter matches any cache directory whose name contains the given
token (case-insensitive). E.g. `Melbourne` matches `1279_Melbourne`.

Skips sessions whose DB is already complete unless `--force` is passed.
Skips any session that's currently live-capturing.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.processing.database import SessionDatabase
from app.processing.preprocessor import SessionPreProcessor
from app.processing.session import SessionManager
from app.services.live_capture import live_capture

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reprocess_event")
logger.setLevel(logging.INFO)


def _is_complete(session_path: Path) -> bool:
    db_file = session_path / "session.db"
    if not db_file.exists():
        return False
    db = SessionDatabase(session_path)
    db.open()
    try:
        return db.get_meta("status") == "complete"
    finally:
        db.close()


async def main(year: int, event_token: str, force: bool) -> int:
    cache_dir = REPO_ROOT / "data" / "livetiming_cache"
    needle = event_token.lower()
    live_files = sorted(
        f for f in cache_dir.glob(f"{year}/*/*/live.jsonl")
        if needle in f.parent.parent.name.lower()
        and "Day_" not in f.parent.name
    )
    total = len(live_files)
    if total == 0:
        logger.error(f"No sessions matched event='{event_token}' in {year}")
        return 1
    logger.info(f"Found {total} sessions for {year} event matching '{event_token}'")

    done = failed = skipped = 0
    overall_start = time.monotonic()
    for idx, live_file in enumerate(live_files, 1):
        session_path = live_file.parent
        rel = session_path.relative_to(cache_dir)

        if live_capture.is_capturing_path(session_path):
            skipped += 1
            logger.info(f"[{idx}/{total}] SKIP {rel} (live capture in progress)")
            continue
        if not force and _is_complete(session_path):
            skipped += 1
            logger.info(f"[{idx}/{total}] SKIP {rel} (already complete)")
            continue

        size_mb = live_file.stat().st_size / (1024 * 1024)
        session_type = SessionManager._infer_session_type(session_path)
        start = time.monotonic()
        logger.info(f"[{idx}/{total}] START {rel} ({size_mb:.1f} MB, type={session_type})")

        preprocessor = SessionPreProcessor(session_path, session_type)
        try:
            await preprocessor.run(tail_follow=False, force=force)
            elapsed = time.monotonic() - start
            done += 1
            logger.info(f"[{idx}/{total}] DONE  {rel} ({elapsed:.1f}s)")
        except Exception as e:
            failed += 1
            logger.exception(f"[{idx}/{total}] FAIL  {rel}: {e}")
        finally:
            preprocessor.close()

    total_elapsed = time.monotonic() - overall_start
    logger.info(
        f"Reprocess event complete: {done} processed, {skipped} skipped, "
        f"{failed} failed in {total_elapsed:.1f}s"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if len(args) < 2:
        print("Usage: python -m utils.scripts.reprocess_event <year> <event_token> [--force]")
        sys.exit(2)
    year = int(args[0])
    event_token = args[1]
    sys.exit(asyncio.run(main(year, event_token, force)))
