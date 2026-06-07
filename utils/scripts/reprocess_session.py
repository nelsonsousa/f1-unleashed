"""Reprocess a single session through SessionPreProcessor.

Run from the repo root:
    python -m utils.scripts.reprocess_session 2026 Melbourne Race
    python -m utils.scripts.reprocess_session 2026 Monte_Carlo Qualifying --force

The event + session tokens are matched case-insensitively against the cache
directory names. E.g. `Melbourne` `Race` matches
`data/livetiming_cache/2026/1279_Melbourne/11234_Race/`.

Refuses to run if the matched session is currently live-capturing.
Skips if the DB is already complete unless `--force` is passed.
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
logger = logging.getLogger("reprocess_session")
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


async def main(year: int, event_token: str, session_token: str, force: bool) -> int:
    cache_dir = REPO_ROOT / "data" / "livetiming_cache"
    ev_needle = event_token.lower()
    ss_needle = session_token.lower()

    matches = sorted(
        f.parent for f in cache_dir.glob(f"{year}/*/*/live.jsonl")
        if ev_needle in f.parent.parent.name.lower()
        and ss_needle in f.parent.name.lower()
    )
    if not matches:
        logger.error(
            f"No session matched event='{event_token}' session='{session_token}' in {year}"
        )
        return 1
    if len(matches) > 1:
        logger.error(
            f"Ambiguous match — {len(matches)} sessions: "
            + ", ".join(str(m.relative_to(cache_dir)) for m in matches)
        )
        return 1

    session_path = matches[0]
    rel = session_path.relative_to(cache_dir)

    if live_capture.is_capturing_path(session_path):
        logger.error(f"SKIP {rel}: live capture in progress")
        return 1
    if not force and _is_complete(session_path):
        logger.info(f"SKIP {rel}: already complete (use --force to re-run)")
        return 0

    size_mb = (session_path / "live.jsonl").stat().st_size / (1024 * 1024)
    session_type = SessionManager._infer_session_type(session_path)
    start = time.monotonic()
    logger.info(f"START {rel} ({size_mb:.1f} MB, type={session_type})")

    preprocessor = SessionPreProcessor(session_path, session_type)
    try:
        await preprocessor.run(tail_follow=False, force=force)
    except Exception as e:
        logger.exception(f"FAIL {rel}: {e}")
        return 2
    finally:
        preprocessor.close()

    elapsed = time.monotonic() - start
    logger.info(f"DONE {rel} ({elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if len(args) < 3:
        print(
            "Usage: python -m utils.scripts.reprocess_session "
            "<year> <event_token> <session_token> [--force]"
        )
        sys.exit(2)
    year = int(args[0])
    event_token = args[1]
    session_token = args[2]
    sys.exit(asyncio.run(main(year, event_token, session_token, force)))
