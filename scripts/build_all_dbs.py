"""Rebuild + persist the transient DB for every cached 2026 session.

Used for the data-validation pass: forces a fresh rebuild with current
processor code so every DB reflects the latest pipeline.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.processing.preprocessor import SessionPreProcessor

CACHE = Path("data/livetiming_cache/2026")


# Skip a session whose live.jsonl was modified within this window — a live
# capture is still writing it; reprocessing would race the capture.
LIVE_SKIP_SECONDS = 3600


def sessions():
    now = time.time()
    for event in sorted(CACHE.iterdir()):
        if not event.is_dir():
            continue
        for sess in sorted(event.iterdir()):
            jsonl = sess / "live.jsonl"
            if not jsonl.exists():
                continue
            age = now - jsonl.stat().st_mtime
            if age < LIVE_SKIP_SECONDS:
                print(f"SKIP (live capture, {age:.0f}s old): {sess.parent.name}/{sess.name}",
                      flush=True)
                continue
            yield sess


async def build_one(path: Path) -> tuple[str, float, str]:
    t0 = time.time()
    try:
        pp = SessionPreProcessor(path, "")
        await pp.run(tail_follow=False, force=True)
        pp.close()
        return (path.name, time.time() - t0, "ok")
    except Exception as e:  # noqa: BLE001
        return (path.name, time.time() - t0, f"ERROR: {e!r}")


async def main():
    paths = list(sessions())
    print(f"Building {len(paths)} sessions", flush=True)
    for i, p in enumerate(paths, 1):
        name = f"{p.parent.name}/{p.name}"
        print(f"[{i}/{len(paths)}] {name} ...", flush=True)
        _, dt, status = await build_one(p)
        print(f"    {status} ({dt:.1f}s)", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
