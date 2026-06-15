"""Audio sync helper.

Anchors a session's commentary.aac so that the first audible sound in the
recording maps to (session_start − GREEN_FLAG_LEAD_MINUTES). The F1 audio
broadcast goes live a few minutes before lights out, with a silent intro;
the user-confirmed convention is that audible content starts exactly 5
minutes before the scheduled session start.

We do not re-encode the file. We only rewrite `audio_info.json:start_utc`
so the front-end's existing formula

    displayed_audio_utc = audio_info.start_utc + audio.currentTime

yields a value that matches the data clock at every offset.

Math
----
Let:
    F   = file's natural start UTC (when ffmpeg first wrote bytes)
    X   = offset of first audible sample in the file (seconds)
    S   = session start UTC (green flag)
    G   = GREEN_FLAG_LEAD_MINUTES * 60   (300s)

We want: at audio.currentTime = X, displayed time = S − G.

Setting start_utc' = S − G − X gives:
    displayed = start_utc' + X = S − G              ✓ first audible
    displayed = start_utc' + X + G = S              ✓ green flag
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

GREEN_FLAG_LEAD_MINUTES = 5
SILENCE_THRESHOLD_DB = -35
SILENCE_MIN_DURATION_S = 2.0
# Cap silence-detection scan length so we don't churn through hours of
# audio looking for content; first-audible should be within ~30 minutes.
DETECT_SCAN_LIMIT_S = 1800
# The F1 opening credits sequence (whoosh + theme) is sustained audible
# content for ~30 s. A "first audible" candidate must be followed by at
# least this many seconds before the next silence period — filters out
# brief broadcast pre-roll noise blips that fooled the previous "first
# silence_end" approach.
MIN_SUSTAINED_AUDIBLE_S = 25.0

# Match `silence_end: 469.759` (or 469 with no decimal); Python's stdlib
# float() accepts both.
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")

# Signature-match parameters
REFERENCE_PCM_PATH = Path(__file__).parent / "audio_reference" / "f1_credits_start.pcm"
SIGNATURE_SAMPLE_RATE = 8000   # Hz (8 kHz mono is plenty for a percussive whoosh)
SIGNATURE_SCAN_LIMIT_S = 7200  # 2 h scan window — F1 captures can start well
                                # before the broadcast (live capture monitor
                                # arms at the first sign of session activity)
# Confidence: peak correlation must exceed N × the median magnitude
# to be accepted in a SINGLE-segment scan. For multi-segment apply_sync
# the best match across all segments wins even if its ratio is below
# this threshold (the user's spec: "credits sound is so distinct, keep
# listening" — don't fall back to silence-detect prematurely).
SIGNATURE_PEAK_RATIO = 6.0
SIGNATURE_ACCEPT_MIN_RATIO = 3.0  # absolute floor when picking across segments


def find_credits_offset(audio_file: Path) -> Optional[float]:
    """Convenience wrapper: signature offset only, with the strict
    confidence threshold. Returns None if no strong match. Use
    `find_credits_offset_with_score` if you want to compare matches
    across multiple segments (the lower acceptance floor applies)."""
    offset, ratio = find_credits_offset_with_score(audio_file)
    if offset is None:
        return None
    if ratio < SIGNATURE_PEAK_RATIO:
        return None
    return offset


def find_credits_offset_with_score(
    audio_file: Path,
    *,
    window_lo_s: float = 0,
    window_hi_s: Optional[float] = None,
) -> tuple[Optional[float], float]:
    """Locate the F1 opening-credits whoosh by FFT cross-correlation
    against the bundled reference PCM. Returns (offset_seconds, ratio).

    `window_lo_s` / `window_hi_s` constrain which lags are considered
    when picking the peak — used by apply_sync to reject false positives
    (e.g. instrumental segments later in the broadcast that happen to
    correlate with the credits clip). The full file is still decoded
    and correlated so the baseline noise level reflects the whole
    recording. Default = unconstrained (whole file).

    `ratio` is peak / median |correlation|; the caller decides what
    threshold to apply. (0.0 when nothing matches at all.)
    """
    if not REFERENCE_PCM_PATH.exists():
        return None, 0.0
    try:
        ref = np.fromfile(REFERENCE_PCM_PATH, dtype=np.int16).astype(np.float32)
    except OSError:
        return None, 0.0
    if len(ref) == 0:
        return None, 0.0

    try:
        out = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-t", str(SIGNATURE_SCAN_LIMIT_S),
                "-i", str(audio_file),
                "-ac", "1", "-ar", str(SIGNATURE_SAMPLE_RATE),
                "-f", "s16le", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=SIGNATURE_SCAN_LIMIT_S + 60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"ffmpeg decode failed for {audio_file}: {e}")
        return None, 0.0

    target = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)
    if len(target) < len(ref) * 2:
        return None, 0.0

    # Normalise (centre + unit-variance) so a louder broadcast can't
    # bias the correlation away from a quieter reference.
    ref_n = ref - ref.mean()
    rs = ref_n.std()
    if rs > 0:
        ref_n = ref_n / rs
    target_n = target - target.mean()
    ts = target_n.std()
    if ts > 0:
        target_n = target_n / ts

    # FFT-based cross-correlation: correlate target with time-reversed
    # reference. Equivalent to scipy.signal.fftconvolve(target, ref[::-1])
    # in 'full' mode but implemented directly to avoid the scipy.signal
    # import overhead.
    n = len(target) + len(ref) - 1
    n_fft = 1 << (n - 1).bit_length()  # next pow-of-2 for speed
    fft_t = np.fft.rfft(target_n, n_fft)
    fft_r = np.fft.rfft(ref_n[::-1], n_fft)
    corr = np.fft.irfft(fft_t * fft_r, n_fft)[:n]

    # Only consider lags where the reference fits inside the target.
    valid = corr[len(ref) - 1: len(target)]
    if len(valid) == 0:
        return None, 0.0

    baseline = float(np.median(np.abs(valid)))
    # Apply the proximity window: only consider peak candidates whose
    # lag falls within [window_lo_s, window_hi_s] (in samples).
    lo_samp = max(0, int(window_lo_s * SIGNATURE_SAMPLE_RATE))
    hi_samp = (len(valid) if window_hi_s is None
               else min(len(valid), int(window_hi_s * SIGNATURE_SAMPLE_RATE)))
    if hi_samp <= lo_samp:
        return None, 0.0
    windowed = valid[lo_samp:hi_samp]
    peak_idx_w = int(np.argmax(windowed))
    peak_idx = lo_samp + peak_idx_w
    peak_val = float(valid[peak_idx])
    ratio = peak_val / baseline if baseline > 0 else 0.0
    offset_s = peak_idx / SIGNATURE_SAMPLE_RATE
    win_desc = (f", window=[{window_lo_s:.0f}, "
                f"{window_hi_s if window_hi_s is not None else 'end'}]s")
    logger.info(
        f"signature scan {audio_file.name}: best at {offset_s:.2f}s "
        f"(peak={peak_val:.0f}, baseline={baseline:.0f}, ratio={ratio:.1f}{win_desc})"
    )
    return offset_s, ratio


def detect_first_audible_offset(audio_file: Path) -> Optional[float]:
    """Return the offset (seconds) of the first non-silent sample, or None
    if the entire scanned window is silent or ffmpeg fails.

    Uses ffmpeg's silencedetect filter; the first reported `silence_end`
    is the moment audible content begins.
    """
    try:
        out = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-t", str(DETECT_SCAN_LIMIT_S),
                "-i", str(audio_file),
                "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={SILENCE_MIN_DURATION_S}",
                "-f", "null", "/dev/null",
            ],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            timeout=DETECT_SCAN_LIMIT_S + 60,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"ffmpeg silencedetect failed for {audio_file}: {e}")
        return None

    text = out.stderr or ""
    ends = [float(m) for m in _SILENCE_END_RE.findall(text)]
    starts = sorted(float(m) for m in _SILENCE_START_RE.findall(text))
    if not ends:
        # No silence_end reported. Either:
        #   (a) the whole scanned window is silent → no audible content yet
        #   (b) audio is loud from frame 0 → no leading silence at all
        if "silence_start: 0" in text:
            return None  # case (a)
        return 0.0       # case (b)

    # Find the first silence_end followed by at least MIN_SUSTAINED_AUDIBLE_S
    # of continuous audible content (no further silence_start within
    # that window) — that filters out brief pre-roll noise blips that
    # appear before the real opening credits / whoosh.
    for end in ends:
        next_silence = next((s for s in starts if s > end), None)
        gap = (next_silence - end) if next_silence is not None else float("inf")
        if gap >= MIN_SUSTAINED_AUDIBLE_S:
            return end
    # Nothing sustained — return the LAST silence_end so the anchor lands
    # in the most-recent audible region rather than the noisiest pre-roll
    # blip; better than returning the first false-positive.
    return ends[-1]


def session_start_utc(session_path: Path) -> Optional[datetime]:
    """Read session start from subscribe.json's SessionInfo.

    StartDate is in local track time (no offset suffix). GmtOffset is
    e.g. "-04:00:00" or "+11:00:00". We combine them into a UTC datetime.
    """
    sub = session_path / "subscribe.json"
    if not sub.exists():
        return None
    try:
        data = json.loads(sub.read_text())
    except json.JSONDecodeError:
        return None

    info = data.get("SessionInfo") or {}
    start_str = info.get("StartDate")
    gmt_str = info.get("GmtOffset", "00:00:00")
    if not start_str:
        return None

    try:
        local = datetime.fromisoformat(start_str)
    except ValueError:
        return None

    sign = -1 if gmt_str.startswith("-") else 1
    parts = gmt_str.lstrip("-+").split(":")
    off_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60
    return local - timedelta(seconds=sign * off_seconds)


def corrected_start_utc(
    audio_file: Path,
    session_start: datetime,
    *,
    grace_minutes: int = GREEN_FLAG_LEAD_MINUTES,
) -> Optional[datetime]:
    """Compute the start_utc that anchors the F1 opening-credits whoosh
    at (session_start − grace_minutes). Returns None if we can't locate
    the whoosh.

    Two detection strategies in order:
      1. **Signature match** (preferred) — FFT cross-correlation against
         a bundled 3-second reference PCM. Robust against commentary
         that fires up before the credits.
      2. **Silence-end fallback** — first sustained-audible region.
         Used when signature confidence is low.
    """
    # Prefer signature match: cross-correlate against the reference clip.
    offset = find_credits_offset(audio_file)
    if offset is None:
        offset = detect_first_audible_offset(audio_file)
    if offset is None:
        return None
    return session_start - timedelta(minutes=grace_minutes) - timedelta(seconds=offset)


def apply_sync(session_path: Path) -> Optional[datetime]:
    """Run the full sync correction for one session.

    Scans EVERY segment with the credits signature and picks the
    strongest match across all of them — the opening credits can land
    anywhere in the recording (live capture often starts well before
    the broadcast). Then anchors `audio_info.001.json`'s `start_utc`
    so that audible content at the matched position displays at
    (scheduled_session_start − 5 min) on the playback clock.

    For multi-segment captures (capture was restarted mid-session) the
    client streams segments oldest-first, so the FIRST segment's
    `start_utc` IS the displayed time of the combined stream's byte 0.
    We compute the combined-stream offset (sum of prior segment
    durations + offset_in_matched_segment) and subtract.

    Returns the new start_utc, or None on no audible match anywhere.
    Idempotent — safe to re-run.
    """
    rotated = sorted(session_path.glob("commentary.[0-9][0-9][0-9].aac"))
    current = session_path / "commentary.aac"
    segments = list(rotated)
    if current.exists() and current.stat().st_size > 0:
        segments.append(current)
    if not segments:
        return None

    session_start = session_start_utc(session_path)
    if session_start is None:
        logger.warning(f"{session_path.name}: no session start — skipping sync")
        return None

    # Compute an EXPECTED credits offset in the combined stream from
    # the first data-message UTC: credits should air at
    # (scheduled_session_start − 5 min), and audio capture starts close
    # to (often same wall-clock instant as) the first data message.
    # Use ±10 min as the acceptance window — wide enough that a
    # broadcaster who fires audio capture early still wins, narrow
    # enough to reject the late-broadcast instrumental sections that
    # correlate spuriously with the credits clip.
    first_msg = _first_data_msg_utc(session_path)
    if first_msg is not None:
        expected = (session_start - timedelta(minutes=GREEN_FLAG_LEAD_MINUTES)
                    - first_msg).total_seconds()
        window_lo = max(0.0, expected - 600)
        window_hi = expected + 600
    else:
        window_lo, window_hi = 0.0, None

    # Scan each segment. Track best (ratio, cumulative_offset_in_stream).
    # The proximity window is applied per-segment, translated into
    # segment-local seconds.
    best_ratio = 0.0
    best_total_offset: Optional[float] = None
    best_seg_name = None
    cumulative = 0.0
    for seg in segments:
        seg_dur = _ffprobe_duration(seg)
        if window_hi is not None:
            seg_lo = max(0.0, window_lo - cumulative)
            seg_hi = window_hi - cumulative
        else:
            seg_lo, seg_hi = 0.0, None
        if seg_hi is None or seg_hi > 0:
            offset, ratio = find_credits_offset_with_score(
                seg, window_lo_s=seg_lo, window_hi_s=seg_hi,
            )
            if offset is not None and ratio > best_ratio:
                best_ratio = ratio
                best_total_offset = cumulative + offset
                best_seg_name = seg.name
        cumulative += seg_dur

    if best_total_offset is None or best_ratio < SIGNATURE_ACCEPT_MIN_RATIO:
        logger.warning(
            f"{session_path.name}: no credits signature match across "
            f"{len(segments)} segment(s) (best ratio {best_ratio:.1f}) — "
            f"skipping sync"
        )
        return None

    new_start = (session_start
                 - timedelta(minutes=GREEN_FLAG_LEAD_MINUTES)
                 - timedelta(seconds=best_total_offset))

    # Write to the FIRST segment's info file (= the stream's byte 0).
    if rotated:
        info_file = session_path / rotated[0].name.replace(
            "commentary", "audio_info"
        ).replace(".aac", ".json")
    else:
        info_file = session_path / "audio_info.json"
    if not info_file.exists():
        return None
    try:
        info = json.loads(info_file.read_text())
    except json.JSONDecodeError:
        info = {}

    info["start_utc"] = new_start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{new_start.microsecond // 1000:03d}Z"
    info["sync_applied"] = True

    info_file.write_text(json.dumps(info, indent=2))
    logger.info(
        f"{session_path.name}: audio sync (matched {best_seg_name} @ "
        f"stream-offset {best_total_offset:.1f}s, ratio {best_ratio:.1f}) "
        f"→ start_utc={info['start_utc']}"
    )
    return new_start


def probe_offset_at(
    session_path: Path,
    probe_pcm: np.ndarray,
    probe_rate: int,
    target_combined_offset_s: float,
    window_s: float = 600.0,
) -> tuple[Optional[float], float]:
    """Locate where ``probe_pcm`` matches inside the combined commentary
    stream, around ``target_combined_offset_s`` (± window_s).

    Returns (matched_combined_offset_s, confidence_ratio). ``None`` for
    the offset means no decode possible (= file missing or empty); a
    confidence ratio < ~3 means the match is unreliable noise.

    The ``target`` is the position the caller thinks the broadcast is
    currently at (= computed from the data clock and audio_info.start_utc).
    We decode a window around that position from the combined commentary
    stream, then cross-correlate the probe against it. The peak gives
    the actual combined-stream offset that matches the TV audio.
    """
    rotated = sorted(session_path.glob("commentary.[0-9][0-9][0-9].aac"))
    current = session_path / "commentary.aac"
    segments = list(rotated)
    if current.exists() and current.stat().st_size > 0:
        segments.append(current)
    if not segments:
        return None, 0.0

    win_lo = max(0.0, target_combined_offset_s - window_s)
    win_hi = target_combined_offset_s + window_s

    # Build the per-segment slice plan: for each segment that overlaps
    # the window, decode the local-time interval and concatenate. Track
    # the combined-stream offset that lines up with the first decoded
    # sample so we can translate the cross-correlation peak back to
    # combined coordinates.
    decoded = []
    decoded_start_combined: Optional[float] = None
    cumulative = 0.0
    for seg in segments:
        seg_dur = _ffprobe_duration(seg)
        seg_combined_lo = cumulative
        seg_combined_hi = cumulative + seg_dur
        cumulative = seg_combined_hi
        if seg_combined_hi < win_lo:
            continue
        if seg_combined_lo > win_hi:
            break
        local_lo = max(0.0, win_lo - seg_combined_lo)
        local_hi = min(seg_dur, win_hi - seg_combined_lo)
        if local_hi <= local_lo:
            continue
        try:
            out = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{local_lo:.3f}",
                    "-t", f"{(local_hi - local_lo):.3f}",
                    "-i", str(seg),
                    "-ac", "1", "-ar", str(probe_rate),
                    "-f", "s16le", "-",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None, 0.0
        pcm = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)
        if pcm.size == 0:
            continue
        if decoded_start_combined is None:
            decoded_start_combined = seg_combined_lo + local_lo
        decoded.append(pcm)

    if not decoded or decoded_start_combined is None:
        return None, 0.0
    target = np.concatenate(decoded)
    ref = np.asarray(probe_pcm, dtype=np.float32)
    if len(target) < len(ref) * 2:
        return None, 0.0

    # Normalise both signals so a loud broadcast or a soft probe can't
    # bias the correlation.
    ref_n = ref - ref.mean()
    rs = ref_n.std()
    if rs > 0:
        ref_n = ref_n / rs
    tgt_n = target - target.mean()
    ts = tgt_n.std()
    if ts > 0:
        tgt_n = tgt_n / ts

    n = len(target) + len(ref) - 1
    n_fft = 1 << (n - 1).bit_length()
    fft_t = np.fft.rfft(tgt_n, n_fft)
    fft_r = np.fft.rfft(ref_n[::-1], n_fft)
    corr = np.fft.irfft(fft_t * fft_r, n_fft)[:n]
    valid = corr[len(ref) - 1: len(target)]
    if len(valid) == 0:
        return None, 0.0
    baseline = float(np.median(np.abs(valid)))
    peak_idx = int(np.argmax(valid))
    peak_val = float(valid[peak_idx])
    ratio = peak_val / baseline if baseline > 0 else 0.0
    matched_local_s = peak_idx / probe_rate
    matched_combined_s = decoded_start_combined + matched_local_s
    # Dump the reference window too so the offline replay has both halves.
    try:
        from app.config import TMP_DIR
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        target.astype(np.int16).tofile(str(TMP_DIR / "last_ref.s16"))
    except Exception:
        pass
    # Stash diagnostics on the function for the router to surface.
    probe_offset_at.last_peak = peak_val
    probe_offset_at.last_baseline = baseline
    probe_offset_at.last_window_s = float(len(target)) / probe_rate
    return matched_combined_s, ratio


def _ffprobe_duration(audio_file: Path) -> float:
    """Return the media duration of an AAC file in seconds (0.0 on error)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(audio_file)],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def _first_data_msg_utc(session_path: Path) -> Optional[datetime]:
    """Return the UTC of the first data message in live.jsonl, or None.

    Used by apply_sync to estimate where the credits SHOULD be in the
    audio file (audio capture starts close to the same wall-clock
    instant as data capture).
    """
    live = session_path / "live.jsonl"
    if not live.exists():
        return None
    try:
        with open(live, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dt = msg.get("DateTime")
                if not dt:
                    continue
                # F1 timestamps are "2026-05-23T19:49:09.223706" (no TZ).
                try:
                    return datetime.fromisoformat(dt.split("+")[0].rstrip("Z"))
                except ValueError:
                    continue
    except OSError:
        return None
    return None


def _earliest_segment(session_path: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Locate the EARLIEST audio segment + its info file in a session.

    Multi-segment captures rotate `commentary.aac → commentary.001.aac`
    (and `audio_info.json → audio_info.001.json`) on each restart, so
    the lowest-numbered segment is the oldest. The current live segment
    is the un-numbered `commentary.aac`; pick it only when there are no
    rotated segments.
    """
    rotated = sorted(session_path.glob("commentary.[0-9][0-9][0-9].aac"))
    if rotated:
        audio = rotated[0]
        # audio_info.001.json mirrors commentary.001.aac.
        ord_str = audio.name.split(".")[1]  # "001"
        info = session_path / f"audio_info.{ord_str}.json"
        return audio, info
    audio = session_path / "commentary.aac"
    info = session_path / "audio_info.json"
    return audio, info
