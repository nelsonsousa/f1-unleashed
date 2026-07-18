"""Team-radio clip download + caching (card 8).

F1 `TeamRadio` messages (captured to live.jsonl, previously unprocessed) carry
`Captures` of `{Utc, RacingNumber, Path}`, where `Path` is relative to the
session's CDN static dir, e.g. "TeamRadio/NOR_1_20260524_124627.mp3". We
download each clip to `{session_path}/TeamRadio/<basename>` so it can be played
back time-aligned to the session clock (with commentary ducking). Transcription
is deferred to 1.3.

`Captures` is a LIST on the initial subscribe and a DICT (keyed by index) on
incremental updates — extract_captures() normalises both.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

LIVETIMING_STATIC_BASE = "https://livetiming.formula1.com/static"
_HEADERS = {"User-Agent": "F1-Timing-App/1.0"}


def extract_captures(data: Any) -> list[dict]:
    """Normalise a TeamRadio message's `Captures` (list on subscribe, dict on
    update) into a list of {Utc, RacingNumber, Path} dicts with a Path."""
    if not isinstance(data, dict):
        return []
    caps = data.get("Captures")
    if isinstance(caps, list):
        items = caps
    elif isinstance(caps, dict):
        items = list(caps.values())
    else:
        return []
    return [c for c in items if isinstance(c, dict) and c.get("Path")]


def clip_url(static_path: str, capture_path: str) -> str:
    """Full CDN URL for a capture Path relative to the session's static dir."""
    return f"{LIVETIMING_STATIC_BASE}/{(static_path or '').strip('/')}/{capture_path.lstrip('/')}"


def clip_dest(cache_path: Path, capture_path: str) -> Optional[Path]:
    """Local cache path — keeps the "TeamRadio/<file>.mp3" sub-structure. Returns None
    (caller skips the clip) if the feed-supplied Path escapes the session cache dir — e.g.
    a "../.." Path — since the bytes fetched from the constructed CDN URL get written here."""
    dest = (cache_path / capture_path.lstrip("/")).resolve()
    if not dest.is_relative_to(cache_path.resolve()):
        logger.warning("team radio: skipping capture with out-of-cache Path %r", capture_path)
        return None
    return dest


async def download_clip(session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
    """Download one clip to `dest` (skip if already cached). Returns True only on
    a fresh successful download. Writes via a .part temp then renames (atomic)."""
    if dest.exists() and dest.stat().st_size > 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"team radio HTTP {resp.status} for {url}")
                return False
            body = await resp.read()
        if not body:
            return False
        tmp.write_bytes(body)
        tmp.rename(dest)
        return True
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
        logger.warning(f"team radio download failed {url}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


async def download_captures(cache_path: Path, static_path: str, captures: list[dict],
                            session: Optional[aiohttp.ClientSession] = None,
                            concurrency: int = 4) -> int:
    """Download every not-yet-cached clip in `captures`. Returns the count of
    fresh downloads. Creates its own aiohttp session if none is supplied."""
    if not static_path or not captures:
        return 0
    own = session is None
    if own:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60), headers=_HEADERS)
    sem = asyncio.Semaphore(concurrency)
    count = 0

    async def _one(cap: dict) -> None:
        nonlocal count
        path = cap.get("Path")
        if not path:
            return
        dest = clip_dest(cache_path, path)
        if dest is None:
            return
        async with sem:
            if await download_clip(session, clip_url(static_path, path), dest):
                count += 1

    try:
        await asyncio.gather(*(_one(c) for c in captures))
    finally:
        if own:
            await session.close()
    return count


async def backfill_session(cache_path: Path) -> int:
    """Read a cached session's live.jsonl, extract the static Path + every
    TeamRadio capture, and download any clips not already cached. Returns the
    number of fresh downloads. (Lets existing sessions be backfilled and is the
    test path for the capture-time download.)"""
    live = cache_path / "live.jsonl"
    if not live.exists():
        return 0
    static_path: Optional[str] = None
    captures: list[dict] = []
    seen: set[str] = set()

    def _json(m: dict) -> dict:
        d = m.get("Json")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except json.JSONDecodeError:
                return {}
        return d if isinstance(d, dict) else {}

    with live.open(encoding="utf-8") as f:
        for line in f:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = m.get("Type")
            if t == "SessionInfo" and static_path is None:
                p = _json(m).get("Path")
                if p:
                    static_path = p
            elif t == "TeamRadio":
                for cap in extract_captures(_json(m)):
                    p = cap.get("Path")
                    if p and p not in seen:
                        seen.add(p)
                        captures.append(cap)
    if not static_path or not captures:
        return 0
    return await download_captures(cache_path, static_path, captures)
