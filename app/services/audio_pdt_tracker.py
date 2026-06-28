"""HLS PROGRAM-DATE-TIME side-car tracker (segment-pinned anchoring).

Anchors `audio_info.json:start_utc` to the broadcast wall-clock UTC of the
audio's **byte 0** — the `#EXT-X-PROGRAM-DATE-TIME` of the first HLS segment
ffmpeg captured.

Why (card zpn5J5U4)
-------------------
The old approach tagged start_utc to ffmpeg's launch wall-clock and then
re-anchored via `edge_pdt − file_duration`. Both are imprecise — ffmpeg's
launch instant is NOT the broadcast time of byte 0, and edge−duration samples
two loosely-coupled quantities — giving a per-session-variable offset
(seen 28-34 s).

The fix: the timestamp we need already exists per segment (PDT). ffmpeg is
launched with `-live_start_index -3`, so byte 0 is the 3rd segment from the
live edge. We read that segment's PDT from the playlist and use it directly:

    start_utc = PDT(segments[-3])

This is exact by construction (the PDT *is* that segment's broadcast time);
the only error is the sub-second playlist-advance race between ffmpeg opening
the stream and our first poll (≤ ~1 segment ≈ 2 s), versus the prior 28-34 s.

Note on why we DON'T frame-match: the audio is a gapless concatenation and
AAC-LC is a fixed 1024 samples/frame, so one could try to identify byte 0 by
matching the file's frame count to a run of segment frames. But with
near-uniform segment sizes that is mathematically underdetermined
(start X / edge Y is indistinguishable from start X−k / edge Y−k). So we pin
the start from the playlist instead. The client then maps any position exactly
via `start_utc + frame_index / sample_rate` (continuous capture) — the frame
map is exact client-side without server frame matching.

A per-segment ledger (PDT + EXTINF + discontinuity flag) is still written for
audit and to flag discontinuities, which would break the single-anchor linear
map (rare for the rdio commentary feed).

Falls back to `edge_pdt − file_seconds` (exact frame-based duration) only if
the playlist never exposes a PDT. Tracker is a daemon thread; one instance per
ffmpeg run. Failures are logged and swallowed; capture is never blocked.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

START_POLL_S = 1.0            # poll fast until anchored
STEADY_POLL_S = 5.0          # keep the live-edge cap (pdt_map.jsonl) fresh
FF_TIMEOUT_S = 20.0          # no ffmpeg-logged segment by now → segs[-3] fallback
START_TIMEOUT_S = 60.0       # still no PDT → edge−duration fallback
PLAYLIST_FAIL_TIMEOUT_S = 90
LIVE_START_INDEX = 3         # ffmpeg -live_start_index -3 → byte 0 = segs[-3] (fallback)
SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
                22050, 16000, 12000, 11025, 8000, 7350]

# `Opening '<url>' for reading` lines in ffmpeg's verbose log.
_OPEN_RE = re.compile(r"Opening '([^']+)' for reading")
_SEG_EXT = (".aac", ".ts", ".m4s", ".mp4")


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_pdt(raw: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None) - dt.utcoffset()
    return dt


def _count_adts_frames(data: bytes, start: int = 0):
    """Count whole ADTS frames from `start`.

    Returns (frame_count, consumed_bytes, sample_rate); `consumed_bytes` is a
    frame boundary so callers can resume incrementally. Resyncs on corruption.
    """
    n = len(data)
    i = start
    frames = 0
    sr = 0
    consumed = start
    while i + 7 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            j = data.find(0xFF, i + 1)
            if j == -1:
                break
            i = j
            continue
        frame_len = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        if frame_len < 7:
            i += 1
            continue
        if i + frame_len > n:
            break
        if not sr:
            idx = (data[i + 2] >> 2) & 0x0F
            sr = SAMPLE_RATES[idx] if idx < len(SAMPLE_RATES) else 0
        frames += 1
        i += frame_len
        consumed = i
    return frames, consumed, sr


class PdtTracker(threading.Thread):
    """Polls the audio HLS sub-playlist and pins start_utc to byte-0 PDT."""

    def __init__(self, master_url: str, cache_path: Path):
        super().__init__(daemon=True, name=f"PdtTracker({cache_path.name})")
        self.master_url = master_url
        self.cache_path = Path(cache_path)
        self._stop_evt = threading.Event()
        self._sub_url: Optional[str] = None
        self._base = ""
        self._last_success_ts = time.time()
        self._t0 = time.time()
        self._ff_log = self.cache_path / "audio_ffmpeg.log"
        self._pdt_map: dict[str, dict] = {}   # segment filename → {seq, pdt, dur, disc}

        self._anchored = False
        self._start_seq: Optional[int] = None
        # Ledger from the start segment onward: [{seq, pdt, dur, disc}]
        self._ledger: list[dict] = []
        self._ledger_seqs: set[int] = set()
        # Incremental file frame count (for the no-PDT fallback only).
        self._file_off = 0
        self._file_frames = 0
        self._file_sr = 0
        self._anchor_max: Optional[datetime] = None

    def stop(self) -> None:
        self._stop_evt.set()

    # ─── HLS plumbing ──────────────────────────────────────────────

    def _resolve_sub_playlist(self) -> Optional[str]:
        try:
            resp = requests.get(self.master_url, timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"PdtTracker: master playlist fetch failed: {e}")
            return None
        text = resp.text
        if "#EXTINF" in text:
            return self.master_url
        base = self.master_url.rsplit("/", 1)[0] + "/"
        sub = None
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sub = line
        if not sub:
            return None
        return sub if sub.startswith("http") else (base + sub)

    def _parse_media_playlist(self, text: str):
        """→ [{seq, pdt, dur, disc}] with PDT carry-forward (PDT often appears
        once at the top; later segments inherit prev_pdt + prev_dur)."""
        media_seq = 0
        out = []
        pend_pdt = None
        pend_dur = None
        disc = False
        last_pdt = None
        last_dur = 0.0
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_seq = int(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pend_pdt = _parse_pdt(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
                disc = True
            elif line.startswith("#EXTINF:"):
                try:
                    pend_dur = float(line.split(":", 1)[1].split(",")[0])
                except ValueError:
                    pend_dur = None
            elif line and not line.startswith("#"):
                pdt = pend_pdt
                if pdt is None and last_pdt is not None:
                    pdt = last_pdt + timedelta(seconds=last_dur)
                dur = pend_dur if pend_dur is not None else 0.0
                uri = line.rsplit("/", 1)[-1].split("?")[0]   # basename
                out.append({"seq": media_seq + len(out), "uri": uri,
                            "pdt": pdt, "dur": dur, "disc": disc})
                if pdt is not None:
                    last_pdt, last_dur = pdt, dur
                pend_pdt = None
                pend_dur = None
                disc = False
        return out

    # ─── File frame counting (no-PDT fallback only) ────────────────

    def _update_file_frames(self) -> None:
        f = self.cache_path / "commentary.aac"
        try:
            if f.stat().st_size <= self._file_off:
                return
            with open(f, "rb") as fh:
                fh.seek(self._file_off)
                data = fh.read()
        except OSError:
            return
        frames, consumed, sr = _count_adts_frames(data, 0)
        self._file_frames += frames
        self._file_off += consumed
        if sr and not self._file_sr:
            self._file_sr = sr

    # ─── Anchor + ledger ───────────────────────────────────────────

    def _ffmpeg_first_segment(self) -> Optional[str]:
        """Basename of the first media segment ffmpeg opened (its byte 0), from
        the verbose ffmpeg log; None until ffmpeg has logged it."""
        try:
            txt = self._ff_log.read_text(errors="ignore")
        except OSError:
            return None
        for m in _OPEN_RE.finditer(txt):
            base = m.group(1).rsplit("/", 1)[-1].split("?")[0]
            if base.endswith(_SEG_EXT) and not base.endswith(".m3u8"):
                return base
        return None

    def _anchor_from_ffmpeg(self) -> bool:
        """Anchor to the EXACT segment ffmpeg opened (race-free). Needs both the
        ffmpeg log line and that segment's PDT in the tailed map."""
        first = self._ffmpeg_first_segment()
        if not first:
            return False
        seg = self._pdt_map.get(first)
        if not seg or seg["pdt"] is None:
            return False                      # not in the tailed window yet
        self._start_seq = seg["seq"]
        self._anchored = True
        logger.info(f"PdtTracker: anchored byte-0 to ffmpeg's first segment "
                    f"{first} pdt={_iso_z(seg['pdt'])}")
        self._write_anchor(seg["pdt"], method="ffmpeg-first-segment")
        return True

    def _anchor_from_segs3(self, segs: list) -> bool:
        """Fallback: pin start_utc = PDT(segs[-3]) (ffmpeg's default start) when
        the ffmpeg log is unavailable. Subject to the launch race (~±1 seg)."""
        if len([s for s in segs if s["pdt"] is not None]) < LIVE_START_INDEX:
            return False
        start = segs[-LIVE_START_INDEX]
        if start["pdt"] is None:
            return False
        self._start_seq = start["seq"]
        self._anchored = True
        logger.warning(f"PdtTracker: ffmpeg log unavailable — fell back to segs[-3] "
                       f"seq={start['seq']} pdt={_iso_z(start['pdt'])}")
        self._write_anchor(start["pdt"], method="segs3-fallback")
        return True

    def _extend_ledger(self, segs: list) -> None:
        if self._start_seq is None:
            return
        changed = False
        for s in segs:
            if s["seq"] < self._start_seq or s["seq"] in self._ledger_seqs or s["pdt"] is None:
                continue
            self._ledger.append({"seq": s["seq"], "pdt": _iso_z(s["pdt"]),
                                 "dur": s["dur"], "disc": s["disc"]})
            self._ledger_seqs.add(s["seq"])
            changed = True
            if s["disc"] and s["seq"] > self._start_seq:
                logger.warning(f"PdtTracker: discontinuity at seq={s['seq']} — "
                               "linear anchor may drift across it (piecewise map TODO).")
        if changed:
            self._write_ledger()

    def _fallback_anchor(self, segs: list) -> None:
        """edge_pdt − exact file duration (running max). Only used if no PDT."""
        self._update_file_frames()
        if not self._file_sr or self._file_frames <= 0:
            return
        edge = None
        for s in segs:
            if s["pdt"] is not None:
                edge = s["pdt"] + timedelta(seconds=s["dur"])
        if edge is None:
            return
        anchor = edge - timedelta(seconds=self._file_frames * 1024 / self._file_sr)
        if self._anchor_max is not None and anchor <= self._anchor_max:
            return
        self._anchor_max = anchor
        self._write_anchor(anchor, method="edge-duration-fallback")

    # ─── Persistence ───────────────────────────────────────────────

    def _write_anchor(self, anchor: datetime, method: str) -> None:
        info_file = self.cache_path / "audio_info.json"
        try:
            info = json.loads(info_file.read_text()) if info_file.exists() else {}
        except (json.JSONDecodeError, OSError):
            info = {}
        info["start_utc"] = _iso_z(anchor)
        info["pdt_anchored"] = True
        info["pdt_method"] = method
        info["pdt_anchored_at_ms"] = int(time.time() * 1000)
        try:
            info_file.write_text(json.dumps(info, indent=2))
        except OSError as e:
            logger.warning(f"PdtTracker: audio_info.json write failed: {e}")

    def _write_edge(self, segs: list) -> None:
        """Append the broadcast live edge to pdt_map.jsonl — the signal the
        server's live-edge cap (_audio_edge_offset) reads to hold the data clock
        back to the audio edge. Without this the cap no-ops and data outruns
        audio (the ~75 s lag). Edge = newest segment PDT + its duration."""
        edge = None
        for s in segs:
            if s["pdt"] is not None:
                edge = s["pdt"] + timedelta(seconds=s["dur"])
        if edge is None:
            return
        try:
            with open(self.cache_path / "pdt_map.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"wall_ms": int(time.time() * 1000),
                                    "edge_pdt_utc": _iso_z(edge)}) + "\n")
        except OSError as e:
            logger.warning(f"PdtTracker: pdt_map.jsonl write failed: {e}")

    def _write_ledger(self) -> None:
        try:
            (self.cache_path / "pdt_ledger.json").write_text(json.dumps({
                "start_seq": self._start_seq,
                "start_utc": self._ledger[0]["pdt"] if self._ledger else None,
                "segments": self._ledger,
            }, indent=2))
        except OSError as e:
            logger.warning(f"PdtTracker: pdt_ledger.json write failed: {e}")

    # ─── Main loop ─────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(f"PdtTracker started for {self.cache_path.name}")
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
        self._base = self._sub_url.rsplit("/", 1)[0] + "/"

        wait_s = 0.3
        gave_up = False
        while not self._stop_evt.is_set():
            if self._stop_evt.wait(wait_s):
                break
            wait_s = STEADY_POLL_S if self._anchored else START_POLL_S

            try:
                resp = requests.get(self._sub_url, timeout=5)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.debug(f"PdtTracker: playlist fetch failed: {e}")
                if time.time() - self._last_success_ts > PLAYLIST_FAIL_TIMEOUT_S:
                    logger.warning("PdtTracker: playlist unreachable too long, stopping")
                    return
                continue
            self._last_success_ts = time.time()

            try:
                segs = self._parse_media_playlist(resp.text)
                self._write_edge(segs)          # feed the server's live-edge cap
                # Tail: hold filename→PDT so we have ffmpeg's first segment's PDT
                # even as the window slides (only needed until anchored).
                if not self._anchored:
                    for s in segs:
                        if s["pdt"] is not None:
                            self._pdt_map[s["uri"]] = s

                if not self._anchored and not gave_up:
                    dt = time.time() - self._t0
                    if self._anchor_from_ffmpeg():
                        self._pdt_map.clear()
                    elif dt > FF_TIMEOUT_S and self._anchor_from_segs3(segs):
                        self._pdt_map.clear()
                    elif dt > START_TIMEOUT_S:
                        logger.warning("PdtTracker: no PDT available — "
                                       "using edge−duration fallback.")
                        gave_up = True

                if self._anchored:
                    self._extend_ledger(segs)
                elif gave_up:
                    self._fallback_anchor(segs)
            except Exception as e:
                logger.warning(f"PdtTracker: poll failed: {e}")

        logger.info(f"PdtTracker stopped for {self.cache_path.name}")
