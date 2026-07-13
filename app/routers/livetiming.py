"""
API endpoints for raw F1 Live Timing data.

Provides endpoints to:
- List available meetings/sessions from F1's livetiming service
- Download raw timing data for replay
- List cached sessions
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.services.live_capture import live_capture
from app.services.livetiming_fetcher import livetiming_fetcher
from app.processing.session import session_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/livetiming", tags=["livetiming"])


class MeetingResponse(BaseModel):
    key: int
    number: int  # Event number (0 for testing, 1-24 for races)
    name: str
    official_name: str
    location: str
    country: str
    circuit: str
    sessions: list[dict[str, Any]]


class SessionFetchRequest(BaseModel):
    year: int
    meeting_name: str
    session_type: str
    force: bool = False
    meeting_key: Optional[int] = None


class SessionFetchResponse(BaseModel):
    success: bool
    message: str
    cache_path: Optional[str] = None


class CachedSessionResponse(BaseModel):
    name: str
    path: str
    year: Optional[str] = None
    meeting: Optional[str] = None
    location: Optional[str] = None
    session: Optional[str] = None
    session_type: Optional[str] = None
    session_key: Optional[str] = None
    size_mb: Optional[float] = None
    modified: Optional[str] = None
    has_jsonl: Optional[bool] = None
    has_audio: Optional[bool] = None
    # Present/absent status for the data, audio, and weather files (card).
    data_status: Optional[str] = None
    audio_status: Optional[str] = None
    weather_status: Optional[str] = None


@router.get("/meetings/{year}", response_model=list[MeetingResponse])
async def get_meetings(year: int):
    """Get all meetings (Grand Prix weekends) for a year."""
    try:
        meetings = await livetiming_fetcher.get_meetings(year)
        logger.info(f"Fetched {len(meetings)} meetings for {year}")
        return [
            MeetingResponse(
                key=m.key,
                number=m.number,
                name=m.name,
                official_name=m.official_name,
                location=m.location,
                country=m.country,
                circuit=m.circuit,
                sessions=[
                    {
                        "key": s.key,
                        "name": s.name,
                        "type": s.type,
                        "path": s.path,
                        "start_date": s.start_date.isoformat() if s.start_date else None,
                        "end_date": s.end_date.isoformat() if s.end_date else None,
                        "has_data": s.path is not None,
                    }
                    for s in m.sessions
                ],
            )
            for m in meetings
        ]
    except aiohttp.ClientResponseError as e:
        logger.error(f"HTTP error fetching meetings for {year}: {e.status} {e.message}")
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"No F1 data available for {year}")
        raise HTTPException(status_code=e.status, detail=str(e))
    except aiohttp.ClientError as e:
        logger.error(f"Network error fetching meetings for {year}: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to connect to F1 Live Timing servers: {e}")
    except Exception as e:
        logger.error(f"Failed to get meetings for {year}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/meetings/{year}/debug")
async def get_meetings_debug(year: int):
    """Debug endpoint to see raw API response for meetings."""
    import aiohttp as aio
    import json
    url = f"https://livetiming.formula1.com/static/{year}/Index.json"
    async with aio.ClientSession() as session:
        async with session.get(url) as response:
            text = await response.text()
            if text.startswith('\ufeff'):
                text = text[1:]
            try:
                data = json.loads(text)
                meetings = data.get("Meetings", [])
                return {
                    "url": url,
                    "status": response.status,
                    "total_meetings": len(meetings),
                    "meeting_names": [m.get("Name", "unknown") for m in meetings],
                }
            except json.JSONDecodeError:
                return {
                    "url": url,
                    "status": response.status,
                    "error": "Invalid JSON response from F1 API",
                    "raw_response_preview": text[:500] if text else "(empty)",
                }


@router.get("/cached", response_model=list[CachedSessionResponse])
async def get_cached_sessions():
    """Get list of all cached raw timing sessions."""
    sessions = livetiming_fetcher.get_cached_sessions()
    return [CachedSessionResponse(**s) for s in sessions]


@router.post("/fetch", response_model=SessionFetchResponse)
async def fetch_session(request: SessionFetchRequest):
    """
    Download raw timing data for a session.

    This downloads all .jsonStream files from F1's livetiming service
    and stores them locally for replay.
    """
    try:
        result = await livetiming_fetcher.get_session_by_name(
            request.year, request.meeting_name, request.session_type,
            meeting_key=request.meeting_key,
        )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {request.year} {request.meeting_name} {request.session_type}",
            )

        meeting, session = result

        # Check if already cached
        if not request.force and livetiming_fetcher.is_session_cached(
            request.year, meeting, session
        ):
            cache_dir = livetiming_fetcher._get_session_cache_dir(
                request.year, meeting, session
            )
            return SessionFetchResponse(
                success=True,
                message=f"Session already cached",
                cache_path=str(cache_dir),
            )

        # Fetch the session
        cache_path = await livetiming_fetcher.fetch_session(
            request.year, meeting, session, request.force
        )

        return SessionFetchResponse(
            success=True,
            message=f"Successfully downloaded {meeting.location} {session.name}",
            cache_path=str(cache_path),
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fetch/stream")
async def fetch_session_stream(
    year: int = Query(...),
    meeting_name: str = Query(...),
    session_type: str = Query(...),
    force: bool = Query(False),
    meeting_key: Optional[int] = Query(None),
):
    """
    Download raw timing data with progress streaming via SSE.

    Returns Server-Sent Events with progress updates as topics are downloaded.
    """

    async def generate():
        try:
            result = await livetiming_fetcher.get_session_by_name(
                year, meeting_name, session_type, meeting_key=meeting_key,
            )

            if not result:
                yield {
                    "event": "error",
                    "data": f"Session not found: {year} {meeting_name} {session_type}",
                }
                return

            meeting, session = result

            # Check if already cached
            if not force and livetiming_fetcher.is_session_cached(year, meeting, session):
                cache_dir = livetiming_fetcher._get_session_cache_dir(year, meeting, session)
                yield {
                    "event": "complete",
                    "data": {
                        "message": "Session already cached",
                        "cache_path": str(cache_dir),
                    },
                }
                return

            yield {
                "event": "start",
                "data": {
                    "meeting": meeting.name,
                    "location": meeting.location,
                    "session": session.name,
                },
            }

            # Progress callback
            progress_queue = asyncio.Queue()

            def progress_callback(topic: str, status: str):
                try:
                    progress_queue.put_nowait({"topic": topic, "status": status})
                except asyncio.QueueFull:
                    pass

            # Start fetch in background
            fetch_task = asyncio.create_task(
                livetiming_fetcher.fetch_session(
                    year, meeting, session, force, progress_callback
                )
            )

            # Stream progress updates
            while not fetch_task.done():
                try:
                    progress = await asyncio.wait_for(
                        progress_queue.get(), timeout=0.5
                    )
                    yield {"event": "progress", "data": progress}
                except asyncio.TimeoutError:
                    continue

            # Drain remaining progress
            while not progress_queue.empty():
                progress = progress_queue.get_nowait()
                yield {"event": "progress", "data": progress}

            # Get result
            try:
                cache_path = await fetch_task
                yield {
                    "event": "complete",
                    "data": {
                        "message": f"Successfully downloaded {meeting.location} {session.name}",
                        "cache_path": str(cache_path),
                    },
                }
            except Exception as e:
                yield {"event": "error", "data": str(e)}

        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(generate())


@router.get("/session/{year}/{meeting_name}/{session_type}/info")
async def get_session_info(year: int, meeting_name: str, session_type: str):
    """
    Get information about a session without downloading.

    Returns session metadata and whether it's cached.
    """
    try:
        result = await livetiming_fetcher.get_session_by_name(
            year, meeting_name, session_type
        )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {year} {meeting_name} {session_type}",
            )

        meeting, session = result
        is_cached = livetiming_fetcher.is_session_cached(year, meeting, session)

        cache_info = None
        if is_cached:
            cache_dir = livetiming_fetcher._get_session_cache_dir(year, meeting, session)
            live_file = cache_dir / "live.jsonl"
            if live_file.exists():
                stat = live_file.stat()
                cache_info = {
                    "path": str(cache_dir),
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified": stat.st_mtime,
                }

        return {
            "meeting": {
                "key": meeting.key,
                "name": meeting.name,
                "location": meeting.location,
                "country": meeting.country,
            },
            "session": {
                "key": session.key,
                "name": session.name,
                "type": session.type,
                "path": session.path,
                "has_data": session.path is not None,
                "start_date": session.start_date.isoformat() if session.start_date else None,
            },
            "cached": is_cached,
            "cache_info": cache_info,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _ordered_audio_segments(session_path) -> list:
    """Return rotated audio segments + the live one, in chronological order.

    Each capture rotation writes commentary.NNN.aac (1, 2, 3, ...) and the
    current live recording is commentary.aac. Older indices were captured
    first, so concatenating in (NNN..., live) order yields chronological
    audio. ADTS AAC supports byte-level concatenation, so the browser can
    decode the result as a single stream.
    """
    segs = sorted(session_path.glob("commentary.[0-9][0-9][0-9].aac"))
    current = session_path / "commentary.aac"
    if current.exists():
        segs.append(current)
    return segs


def _build_concat_aac(session_path, segments):
    """Concatenate the replay segments into ONE range-seekable AAC, cached as
    `_combined.aac`. Lets the client set audio.currentTime exactly (matching
    clockToAudioSec's cumulative-duration map) instead of the imprecise
    avg-bitrate ?t= byte estimate on the non-seekable chunked concat (which is
    skewed by near-silent bookend segments → audio mis-positions on replay).

    ffmpeg's concat demuxer with `-c copy` splices at ADTS frame boundaries
    (clean seams, no re-encode). Rebuilt when any segment is newer than the
    cache. Returns the cached path, or None on failure (caller falls back to
    the chunked path). Replay-only — never used for live (single segment).
    """
    import subprocess
    out = session_path / "_combined.aac"
    try:
        newest = max(s.stat().st_mtime for s in segments)
        if out.exists() and out.stat().st_size > 0 and out.stat().st_mtime >= newest:
            return out
        list_path = session_path / "_combined.concat.txt"
        list_path.write_text(
            "".join(f"file '{s.resolve()}'\n" for s in segments)
        )
        try:
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(list_path),
                 "-c", "copy", str(out)],
                capture_output=True, text=True, timeout=120,
            )
        finally:
            list_path.unlink(missing_ok=True)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
        logger.warning("concat AAC build failed (rc=%s) for %s: %s",
                       r.returncode, session_path.name, (r.stderr or "")[:200])
    except Exception:
        logger.exception("concat AAC build error for %s", session_path.name)
    return None


@router.get("/teamradio/{session_name}/{filename}")
async def get_team_radio(session_name: str, filename: str):
    """Serve a cached team-radio mp3 clip (card 8). Clips live under
    {session}/TeamRadio/<file>.mp3 (downloaded at capture time / backfilled)."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Bad filename")
    session_path = session_manager._find_session_path(session_name)
    if not session_path or not session_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    clip = session_path / "TeamRadio" / filename
    if not clip.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(str(clip), media_type="audio/mpeg", filename=filename)


def _serve_audio_range(path, request: Request) -> Response:
    """Byte-range serve a (possibly growing) AAC file for the MSE client.

    Reads the CURRENT size on every request, so a LIVE file's growth is picked
    up by the client's incremental Range fetches — this is what lets live audio
    behave EXACTLY like a replay (natively seekable, no chunked `?t=` estimate).
    The client (Route A) transmuxes the ADTS bytes → fMP4 in-browser and feeds
    MSE. Returns 416 when the requested start is past EOF (no new bytes yet →
    the client waits and retries)."""
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        data = path.read_bytes()
        return Response(content=data, media_type="audio/aac",
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(len(data))})
    m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not m:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    if start >= file_size:
        # No new bytes yet (live edge) — tell the client to wait and retry.
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    end = min(end, file_size - 1)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start + 1)
    return Response(content=data, status_code=206, media_type="audio/aac",
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(len(data)),
                    })


def _serve_concat_range(segments, request: Request) -> Response:
    """Byte-range serve a VIRTUAL concatenation of multiple AAC segments.

    The rotated segments (commentary.NNN.aac) have fixed sizes; the current
    `commentary.aac` is still growing in live — its size is re-read every
    request, so the total grows and the client's incremental Range fetches pick
    up new audio (live behaves exactly like replay). A byte range is satisfied
    by reading across whichever segment files it spans. Concatenating raw ADTS
    is frame-safe (the muxer resyncs on the 0xFFF syncword at each seam), and
    the client's per-segment byte-0 anchor map (clockToAudioSec) lines up with
    the concatenated frame stream. Single-segment sessions use _serve_audio_range.
    """
    sizes = [s.stat().st_size for s in segments]
    total = sum(sizes)
    starts, acc = [], 0
    for sz in sizes:
        starts.append(acc)
        acc += sz
    range_header = request.headers.get("range")
    if not range_header:
        data = b"".join(s.read_bytes() for s in segments)
        return Response(content=data, media_type="audio/aac",
                        headers={"Accept-Ranges": "bytes", "Content-Length": str(len(data))})
    m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not m:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else total - 1
    if start >= total:
        # Past the (current) end — live edge, no new bytes yet; client retries.
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    end = min(end, total - 1)
    out = bytearray()
    for seg, seg_start, sz in zip(segments, starts, sizes):
        seg_end = seg_start + sz                       # exclusive
        if seg_end <= start or seg_start > end:
            continue
        rs = max(start, seg_start) - seg_start
        re_end = min(end, seg_end - 1) - seg_start
        with open(seg, "rb") as f:
            f.seek(rs)
            out += f.read(re_end - rs + 1)
    return Response(content=bytes(out), status_code=206, media_type="audio/aac",
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{total}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(len(out)),
                    })


@router.get("/audio/{session_name:path}")
async def get_audio(session_name: str, request: Request):
    """Serve commentary audio for a cached session.

    The session may have multiple ffmpeg-run segments (commentary.NNN.aac
    + the current commentary.aac) if the capture restarted mid-session.
    They are concatenated in chronological order:
      - Live capture in progress  → chunked StreamingResponse: writes all
        rotated segments first, then tail-follows the live one as ffmpeg
        appends bytes.
      - Otherwise (static/replay) → if a single segment exists, serve it
        as a FileResponse (range-seekable); otherwise stream the
        concatenation (loses byte-range support but stays correct).
    """
    session_path = session_manager._find_session_path(session_name)
    if not session_path or not session_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    segments = _ordered_audio_segments(session_path)
    if not segments:
        raise HTTPException(status_code=404, detail="No audio file found for session")

    is_live = live_capture.is_capturing_path(session_path)

    # Live captures: serve ONLY the current `commentary.aac`. Rotated
    # segments (commentary.NNN.aac) were recorded under a previous
    # PdtTracker run with their own broadcast anchor — including them
    # here would mean:
    #   - total_bytes = sum(rotated + live), but
    #   - total_secs  = (now − audio_info.start_utc) anchors ONLY the
    #                   live segment
    # so byterate inflates by ~18×, ?t=N seeks deep into the rotated
    # segment mid-AAC-frame, the decoder chokes at the seam, and the
    # browser ends up with 1-2 s of audio + silence (= Monaco 2026 race
    # symptom). Replays after the session ends use ffprobe per-segment
    # so they can concatenate cleanly.
    # MSE client (Route A) requests ?seekable=1 + HTTP Range. Serve a virtual
    # byte-concatenation of ALL segments (rotated + the growing current one),
    # IDENTICAL for live and replay — the client's per-segment byte-0 anchor map
    # (clockToAudioSec) lines up with the concatenated frame stream, and the last
    # segment's size is re-read each request so live growth is followed. This is
    # handled BEFORE the live-only trim below, so live multi-segment captures
    # (capture restarts) no longer break (client maps all segments, server now
    # serves all of them).
    if request.query_params.get("seekable"):
        if len(segments) == 1:
            return _serve_audio_range(segments[0], request)
        return _serve_concat_range(segments, request)

    # Legacy non-MSE path only: live serves just the current segment (chunked).
    # (The seekable MSE path above already handled live with all segments.)
    if is_live:
        segments = segments[-1:]

    # Client may request a starting offset (?t=SECONDS) — used by the
    # live skip path since chunked streaming doesn't support byte ranges.
    try:
        start_t = max(0, int(request.query_params.get("t", "0") or 0))
    except (TypeError, ValueError):
        start_t = 0

    # Single-segment + replay: simple FileResponse with seek/range support.
    if not is_live and len(segments) == 1 and start_t == 0:
        return FileResponse(
            path=str(segments[0]),
            media_type="audio/aac",
            filename="commentary.aac",
        )

    # Multi-segment REPLAY: serve ONE concatenated, range-seekable AAC so the
    # client can set audio.currentTime exactly (matching clockToAudioSec's
    # cumulative-duration map). The chunked concat below is non-seekable and
    # the avg-bitrate ?t= estimate mis-positions audio (skewed by near-silent
    # bookend segments). Falls through to the chunked path if the build fails.
    if not is_live and len(segments) > 1 and start_t == 0:
        combined = _build_concat_aac(session_path, segments)
        if combined:
            return FileResponse(
                path=str(combined),
                media_type="audio/aac",
                filename="commentary.aac",
            )

    chunk_size = 16 * 1024
    idle_timeout = 60.0

    # When start_t > 0 we estimate the byte offset by avg bitrate
    # (sum of all segment sizes ÷ true recording duration).
    # AAC is roughly CBR so this is good enough.
    #
    # Live captures: duration ≈ (now − audio_info.start_utc) is fine.
    # Non-live replays: that formula returns DAYS off, so probe the
    # actual file duration via ffprobe.
    seek_byte_initial = 0
    seek_segment_index = 0
    if start_t > 0:
        total_bytes = sum(s.stat().st_size for s in segments if s.exists())
        total_secs = None
        if is_live:
            info_file = session_path / "audio_info.json"
            if info_file.exists():
                try:
                    info = json.loads(info_file.read_text())
                    start_utc_str = info.get("start_utc")
                    if start_utc_str:
                        start_dt = datetime.fromisoformat(
                            start_utc_str.replace("Z", "+00:00")
                        )
                        total_secs = (datetime.now(timezone.utc) - start_dt).total_seconds()
                except Exception:
                    pass
        else:
            # Sum each segment's media duration. ffprobe is cheap (~30 ms
            # per AAC file) and gives us the true play length, immune to
            # the wall-clock-since-recording trap.
            import subprocess
            total_secs = 0.0
            for seg in segments:
                try:
                    out = subprocess.run(
                        ["ffprobe", "-v", "error",
                         "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1",
                         str(seg)],
                        capture_output=True, text=True, timeout=10,
                    )
                    total_secs += float(out.stdout.strip() or 0)
                except Exception:
                    pass
            if total_secs <= 0:
                total_secs = None
        if total_secs and total_secs > 0 and total_bytes > 0:
            byterate = total_bytes / total_secs
            seek_byte_target = int(start_t * byterate)
            # Walk segments to find which one contains seek_byte_target.
            acc = 0
            for i, seg in enumerate(segments):
                seg_size = seg.stat().st_size
                if acc + seg_size > seek_byte_target:
                    seek_segment_index = i
                    seek_byte_initial = seek_byte_target - acc
                    break
                acc += seg_size

    async def serve_audio():
        loop = asyncio.get_event_loop()
        # Stream all rotated segments verbatim (oldest first), starting
        # from seek_segment_index (skips earlier segments when ?t= is set).
        rotated = segments[:-1] if is_live else segments
        for i, seg in enumerate(rotated):
            if i < seek_segment_index:
                continue
            try:
                with open(seg, "rb") as f:
                    if i == seek_segment_index and seek_byte_initial:
                        f.seek(seek_byte_initial)
                    while True:
                        if await request.is_disconnected():
                            return
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
            except Exception:
                continue

        if not is_live:
            return

        # Tail-follow the current segment, starting near the live edge so
        # the browser doesn't sit on zero bytes for the 15-30 s between
        # ffmpeg's HLS-segment burst writes.
        live_seg = segments[-1]
        # If ?t= landed in the live segment, seek there; otherwise the
        # default live-edge position.
        if seek_segment_index == len(segments) - 1 and seek_byte_initial:
            seek_byte = seek_byte_initial
        else:
            seek_byte, _ = live_capture.get_live_stream_position(session_path)
        idle_start: Optional[float] = None
        with open(live_seg, "rb") as f:
            f.seek(seek_byte)
            while True:
                if await request.is_disconnected():
                    return
                chunk = f.read(chunk_size)
                if chunk:
                    idle_start = None
                    yield chunk
                    continue
                if not live_capture.is_capturing_path(session_path):
                    return
                now = loop.time()
                if idle_start is None:
                    idle_start = now
                elif now - idle_start > idle_timeout:
                    return
                await asyncio.sleep(0.5)

    return StreamingResponse(
        serve_audio(),
        media_type="audio/aac",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/analysis/pecking_order/{session_name:path}")
async def get_pecking_order(session_name: str):
    """Return the prior session's pecking_order.json (= the previous race
    weekend's last session for FP1, or the prior session of the same
    weekend otherwise). NEVER falls back to this session's own analysis:
    that would leak end-of-session results during replay (= the user is
    replaying as if it were live; the session's own pecking order doesn't
    yet exist at any point during it)."""
    from app.processing import analysis_store
    from app.analysis.pecking_order import find_prior_session

    session_path = session_manager._find_session_path(session_name)
    if not session_path or not session_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    prior = find_prior_session(session_path)
    if prior:
        po = analysis_store.load(prior, "pecking_order")
        if po:
            return po
    raise HTTPException(status_code=404, detail="No pecking order available")


@router.get("/analysis/pit_loss_estimate/{session_name:path}")
async def get_pit_loss_estimate(session_name: str):
    """Return the geometric pit-stop time-loss PREDICTION for this session (the prior
    session's accumulated FP→Q estimate — so playing the Race surfaces the quali-final
    prediction). Low-confidence pre-race; the in-race measurement supersedes it."""
    from app.processing import analysis_store
    from app.analysis.pit_loss_estimate import find_prior_session

    session_path = session_manager._find_session_path(session_name)
    if not session_path or not session_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    prior = find_prior_session(session_path)
    if prior:
        est = analysis_store.load(prior, "pit_loss_estimate")
        if est:
            return est
    raise HTTPException(status_code=404, detail="No pit-loss estimate available")


@router.delete("/cached/{session_name:path}")
async def delete_cached_session(session_name: str):
    """Delete a cached session by its cache key."""
    import shutil

    # Find the actual path (handles both new hierarchical and legacy flat structures)
    cache_path = livetiming_fetcher.find_cached_session_path(session_name)
    if not cache_path or not cache_path.exists():
        raise HTTPException(status_code=404, detail="Cached session not found")

    try:
        shutil.rmtree(cache_path)
        return {"success": True, "message": f"Deleted {session_name}"}
    except Exception as e:
        logger.error(f"Failed to delete cached session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/open-cache-folder")
def open_cache_folder():
    """Open the cache root directory in the OS file explorer (card)."""
    import subprocess
    import sys
    from app.config import CACHE_DIR

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = str(CACHE_DIR)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"success": True, "path": path}
    except Exception as e:
        logger.error(f"Failed to open cache folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))
