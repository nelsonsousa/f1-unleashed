"""HLS PROGRAM-DATE-TIME side-car tracker.

Runs alongside the ffmpeg that records `commentary.aac` and continuously
re-anchors `audio_info.json:start_utc` to the broadcast wall-clock UTC
derived from the HLS playlist's `#EXT-X-PROGRAM-DATE-TIME` tag (= the
authoritative segment-aired UTC). Also writes `pdt_map.jsonl` as an
audit trail of every observation.

Why
---
ffmpeg's launch wall-clock is a poor anchor: HLS segments are buffered
upstream, ffmpeg reconnects can skip segments, and broadcast latency
varies. Each observation here gives us the actual mapping from the
audio file's duration to broadcast UTC:

    start_utc = edge_pdt − file_duration_seconds

So on next page refresh the frontend reads a freshly-corrected anchor.
The Unity-based F1 live timing app does this continuously; this is the
same idea, run as a side-car so we don't have to replace ffmpeg.

Tracker is a daemon thread — survives ffmpeg restarts within the same
session (= caller stops it explicitly via .stop()). Failures are logged
and swallowed; the audio capture itself is never blocked.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Poll cadence. HLS target-duration is typically 6 s; polling every
# 10 s gives ~1.5 segments of headroom and a fresh edge PDT each pass.
POLL_INTERVAL_S = 10

# Bail out if the playlist hasn't been reachable for this long.
PLAYLIST_FAIL_TIMEOUT_S = 90


class PdtTracker(threading.Thread):
    """Polls the audio HLS sub-playlist; rewrites audio_info.json + appends pdt_map.jsonl."""

    def __init__(self, master_url: str, cache_path: Path):
        super().__init__(daemon=True, name=f"PdtTracker({cache_path.name})")
        self.master_url = master_url
        self.cache_path = Path(cache_path)
        self._stop_evt = threading.Event()
        self._sub_url: Optional[str] = None
        self._last_success_ts: float = time.time()

    def stop(self) -> None:
        self._stop_evt.set()

    # ─── HLS bookkeeping ───────────────────────────────────────────

    def _resolve_sub_playlist(self) -> Optional[str]:
        """Fetch master m3u8, return the highest-line audio sub-playlist URL.

        Falls back to the master URL itself if the master IS the media
        playlist (= no #EXT-X-STREAM-INF, just segments)."""
        try:
            resp = requests.get(self.master_url, timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"PdtTracker: master playlist fetch failed: {e}")
            return None
        text = resp.text
        # If the playlist contains segments directly (= it's a media playlist),
        # use it as-is.
        if "#EXTINF" in text:
            return self.master_url
        # Otherwise pick the LAST sub-playlist URL — F1's master lists
        # audio variants and the last one is conventionally highest-bitrate.
        base = self.master_url.rsplit("/", 1)[0] + "/"
        sub = None
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sub = line
        if not sub:
            return None
        return sub if sub.startswith("http") else (base + sub)

    @staticmethod
    def _parse_edge_pdt(playlist_text: str) -> Optional[datetime]:
        """Parse a media m3u8 and return the latest #EXT-X-PROGRAM-DATE-TIME.

        We walk the playlist top-to-bottom; the LAST PDT we see is the
        edge (= newest) segment's anchor."""
        latest: Optional[datetime] = None
        for line in playlist_text.splitlines():
            line = line.strip()
            if not line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                continue
            raw = line.split(":", 1)[1].strip()
            try:
                # Python <3.11 chokes on a literal 'Z' suffix.
                pdt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            # Strip tz to keep parity with the naive UTC datetimes used
            # throughout the rest of the codebase.
            if pdt.tzinfo is not None:
                pdt = pdt.astimezone(tz=None).replace(tzinfo=None)
                # astimezone(None) returns local tz — convert to UTC explicitly.
                # Easier: re-parse with manual offset.
                pdt2 = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                # Subtract the offset to get naive UTC.
                pdt = pdt2.replace(tzinfo=None) - pdt2.utcoffset()
            latest = pdt
        return latest

    @staticmethod
    def _ffprobe_duration(audio_file: Path) -> Optional[float]:
        """Return media duration in seconds, or None on failure."""
        if not audio_file.exists() or audio_file.stat().st_size == 0:
            return None
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(audio_file)],
                capture_output=True, text=True, timeout=10,
            )
            return float(out.stdout.strip() or 0)
        except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
            return None

    # ─── Observation + persistence ────────────────────────────────

    # Last audio_seconds we wrote the anchor for. Used to avoid
    # rewriting audio_info.json:start_utc between ffmpeg bursts:
    # edge_pdt advances at 1× wall-clock but the file's duration is
    # only refreshed when ffmpeg appends a new HLS burst (every
    # 15-30 s). Recomputing `anchor = edge_pdt − audio_seconds`
    # between bursts gives a forward-drifting anchor — server's
    # `?t=N` then maps to a too-late byte, audio plays ahead of data.
    # By updating only when audio_seconds CHANGES, the anchor sits
    # at the "fresh duration" reading right after each burst.
    _last_recorded_audio_seconds: float = -1.0

    def _record_observation(self, edge_pdt: datetime, audio_seconds: float) -> None:
        """Append to pdt_map.jsonl and rewrite audio_info.json start_utc."""
        # Implied byte-0 anchor = edge_pdt − file_duration.
        # (Systematic ~6-15 s HLS-buffer latency stays as a constant offset
        # the user can dial in once via the manual delay input. We don't
        # try to guess it here.)
        anchor = edge_pdt - timedelta(seconds=audio_seconds)
        anchor_iso = anchor.strftime("%Y-%m-%dT%H:%M:%S.") + f"{anchor.microsecond // 1000:03d}Z"
        edge_iso = edge_pdt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{edge_pdt.microsecond // 1000:03d}Z"

        record = {
            "wall_ms": int(time.time() * 1000),
            "audio_seconds": round(audio_seconds, 3),
            "edge_pdt_utc": edge_iso,
            "anchor_start_utc": anchor_iso,
        }
        try:
            with open(self.cache_path / "pdt_map.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.warning(f"PdtTracker: pdt_map.jsonl write failed: {e}")
            return

        # Skip rewriting audio_info.json when ffmpeg hasn't written
        # new bytes since the last anchor. Between bursts, audio_seconds
        # is stale but edge_pdt has advanced — recomputing the anchor
        # would drift it forward by the inter-burst gap, and the server's
        # `?t=N` byterate calculation would then map to a too-late byte.
        if audio_seconds <= self._last_recorded_audio_seconds:
            return
        self._last_recorded_audio_seconds = audio_seconds

        # Rewrite audio_info.json's start_utc so a page refresh picks up
        # the freshest anchor. Preserve other fields (url, file).
        info_file = self.cache_path / "audio_info.json"
        try:
            info = json.loads(info_file.read_text()) if info_file.exists() else {}
        except (json.JSONDecodeError, OSError):
            info = {}
        info["start_utc"] = anchor_iso
        info["pdt_anchored"] = True
        info["pdt_anchored_at_ms"] = record["wall_ms"]
        try:
            info_file.write_text(json.dumps(info, indent=2))
        except OSError as e:
            logger.warning(f"PdtTracker: audio_info.json write failed: {e}")

    # ─── Main loop ─────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(f"PdtTracker started for {self.cache_path.name}")
        # Resolve sub-playlist with brief retries (HLS often needs 1-2 s
        # to spin up at session start).
        for _ in range(6):
            if self._stop_evt.is_set():
                return
            self._sub_url = self._resolve_sub_playlist()
            if self._sub_url:
                break
            self._stop_evt.wait(2)
        if not self._sub_url:
            logger.warning(f"PdtTracker: gave up resolving sub-playlist for {self.master_url}")
            return

        audio_file = self.cache_path / "commentary.aac"

        # First poll happens immediately so audio_info.json gets a
        # PDT-derived anchor before the user even opens the page.
        wait_s = 0.5
        while not self._stop_evt.is_set():
            if self._stop_evt.wait(wait_s):
                return
            wait_s = POLL_INTERVAL_S

            try:
                resp = requests.get(self._sub_url, timeout=5)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.debug(f"PdtTracker: playlist fetch failed: {e}")
                if time.time() - self._last_success_ts > PLAYLIST_FAIL_TIMEOUT_S:
                    logger.warning("PdtTracker: playlist unreachable too long, stopping")
                    return
                continue

            edge_pdt = self._parse_edge_pdt(resp.text)
            if edge_pdt is None:
                logger.debug("PdtTracker: no PDT in playlist (yet)")
                continue

            audio_seconds = self._ffprobe_duration(audio_file)
            if audio_seconds is None or audio_seconds < 1.0:
                # ffmpeg hasn't written enough to probe yet — try next pass.
                continue

            self._last_success_ts = time.time()
            try:
                self._record_observation(edge_pdt, audio_seconds)
            except Exception as e:
                logger.warning(f"PdtTracker: record_observation failed: {e}")

        logger.info(f"PdtTracker stopped for {self.cache_path.name}")
