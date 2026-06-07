"""Re-anchor audio_info.start_utc for every cached session.

Run from repo root:  python -m utils.scripts.apply_audio_sync
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.audio_sync import apply_sync, session_start_utc

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("audio-sync")


def main() -> None:
    cache_dir = Path("data/livetiming_cache")
    sessions = sorted({f.parent for f in cache_dir.rglob("commentary.aac")})
    logger.info(f"Found {len(sessions)} sessions with audio")

    applied = 0
    skipped = 0
    for session_path in sessions:
        rel = session_path.relative_to(cache_dir)
        sess_start = session_start_utc(session_path)
        if sess_start is None:
            logger.info(f"  SKIP {rel} (no session start in subscribe.json)")
            skipped += 1
            continue
        new_start = apply_sync(session_path)
        if new_start is None:
            logger.info(f"  SKIP {rel} (first-audible not detected)")
            skipped += 1
        else:
            offset = (sess_start - new_start).total_seconds()
            logger.info(
                f"  DONE {rel}  start_utc={new_start.isoformat()}  "
                f"(green flag at +{offset:.0f}s)"
            )
            applied += 1

    logger.info(f"Applied: {applied}  Skipped: {skipped}")


if __name__ == "__main__":
    main()
