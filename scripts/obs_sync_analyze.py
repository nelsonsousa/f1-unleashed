"""Measure the official F1 app's audio-vs-data sync from the OBS recording,
and how it drifts — by cross-correlating the recording's audio against our
PDT-anchored commentary.aac and reading the on-screen session countdown.

Method (per sampled video-time v in the recording):
  * A(v)  = where the recording's audio content sits in our commentary.aac,
            which is anchored to broadcast UTC via HLS PROGRAM-DATE-TIME.
            => the broadcast UTC the audio being played at instant v aired.
  * E(v)  = session-elapsed shown on screen = 3600 - countdown(v)   [60-min session]
            => the DATA moment the app is displaying at instant v, whose
               broadcast UTC is data_utc(v) = session_start_utc + E(v).
  * skew(v) = A(v) - data_utc(v)
            = how far the app's AUDIO leads(-)/lags(+) the DATA it shows.
  Reported per window + a linear fit over v (constant offset vs rate drift).

A(v) and E(v) need no timezone math. The stopwatch (anchored to a known laptop
wall-clock instant) is an optional cross-check of the broadcast lag W(v)-A(v).

Inputs:
  --obs        the OBS recording (audio track is cross-correlated)
  --session    our cache session dir (has commentary*.aac + audio_info*.json)
  --ocr-csv    output of obs_clock_ocr.py (countdown + stopwatch readings)

    python scripts/obs_sync_analyze.py --obs REC.mkv \
        --session data/livetiming_cache/2026/1287_Barcelona/11302_Practice_3 \
        --ocr-csv tmp/obs_clocks.csv --interval 600 --probe-dur 60 \
        [--session-start-utc 2026-06-13T...Z] [--sw-start-utc 2026-06-13T...Z]
"""
import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import audio_sync

PROBE_RATE = 8000
SESSION_MINUTES = 60  # countdown starts at 60:00


def parse_utc(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def aac_byte0_utc(session_path):
    _, info_file = audio_sync._earliest_segment(session_path)
    if not info_file:
        return None
    return parse_utc(json.loads(Path(info_file).read_text())["start_utc"])


def correlate_in_file(aac, probe_pcm, rate, target_local_s, window_s):
    """Normalised FFT cross-correlation of probe_pcm against a window of a
    SINGLE aac file (around target_local_s). Returns (matched_local_s, ratio).
    Mirrors audio_sync.probe_offset_at but for one segment with its own PDT
    anchor — avoids the multi-segment combined-stream gap skew (I15)."""
    seg_dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(aac)],
        stdout=subprocess.PIPE, text=True).stdout.strip() or 0)
    lo = max(0.0, target_local_s - window_s)
    hi = min(seg_dur, target_local_s + window_s)
    if hi <= lo:
        return None, 0.0
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", str(aac),
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180)
    target = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)
    ref = np.asarray(probe_pcm, dtype=np.float32)
    if len(target) < len(ref) * 2:
        return None, 0.0
    rn = ref - ref.mean()
    if rn.std() > 0:
        rn = rn / rn.std()
    tn = target - target.mean()
    if tn.std() > 0:
        tn = tn / tn.std()
    n = len(target) + len(ref) - 1
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(tn, nfft) * np.fft.rfft(rn[::-1], nfft), nfft)[:n]
    valid = corr[len(ref) - 1:len(target)]
    if len(valid) == 0:
        return None, 0.0
    base = float(np.median(np.abs(valid)))
    pk = int(np.argmax(valid))
    ratio = float(valid[pk]) / base if base > 0 else 0.0
    return lo + pk / rate, ratio


def seg_info_path(aac):
    """commentary.002.aac -> audio_info.002.json ; commentary.aac -> audio_info.json"""
    parts = aac.name.split(".")
    suffix = f".{parts[1]}" if len(parts) == 3 else ""
    return aac.parent / f"audio_info{suffix}.json"


def decode_probe(obs, t, dur):
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", str(obs),
         "-ac", "1", "-ar", str(PROBE_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180)
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)


def load_ocr(path):
    """Return (cd_model, sw_model): each a (v0, value0, slope) linear model fit
    from the parsed OCR readings, or None. Countdown falls ~1s per real second
    (slope ~ -1); stopwatch rises ~ +1."""
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(r)

    def fit(col):
        pts = [(float(r["video_time_s"]), float(r[col]))
               for r in rows if r.get(col) not in (None, "", "None")]
        if len(pts) < 2:
            return None
        v = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        # robust-ish: drop points >3s off a first linear fit (OCR misreads)
        a, b = np.polyfit(v, y, 1)
        keep = np.abs(y - (a * v + b)) < 3.0
        if keep.sum() >= 2:
            a, b = np.polyfit(v[keep], y[keep], 1)
        return (a, b, int((~keep).sum()))

    return fit("cd_s"), fit("sw_s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", required=True, type=Path)
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--aac", type=Path,
                    help="correlate against THIS single segment (e.g. commentary.002.aac) "
                         "with its own PDT anchor, instead of the combined stream")
    ap.add_argument("--ocr-csv", required=True, type=Path)
    ap.add_argument("--interval", type=float, default=600.0, help="probe every N video-seconds")
    ap.add_argument("--start", type=float, default=120.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--probe-dur", type=float, default=60.0)
    ap.add_argument("--search-window", type=float, default=180.0,
                    help="± s searched in the aac around the data-time prior")
    ap.add_argument("--abs-clock-tz", type=float, default=None,
                    help="if set, the OCR'd clock is an ABSOLUTE local wall clock "
                         "(seconds since local midnight, e.g. our app's LOCAL TIME) "
                         "in this UTC offset (hours, e.g. 2 for CEST) — not a countdown")
    ap.add_argument("--session-start-utc", help="UTC of countdown=60:00 (else derived from session)")
    ap.add_argument("--sw-start-utc", help="UTC of stopwatch 00:00 (optional broadcast-lag cross-check)")
    args = ap.parse_args()

    if args.aac:
        info = seg_info_path(args.aac)
        aac_start = parse_utc(json.loads(info.read_text())["start_utc"])
        print(f"correlating against single segment {args.aac.name} "
              f"(PDT anchor {aac_start.strftime('%H:%M:%S.%f')[:-3]}Z)")
    else:
        aac_start = aac_byte0_utc(args.session)
    if aac_start is None:
        print("ERROR: no commentary audio_info/start_utc in session.")
        return

    if args.session_start_utc:
        sess_start = parse_utc(args.session_start_utc)
    else:
        sess_start = audio_sync.session_start_utc(args.session)
    if sess_start is None:
        print("ERROR: could not derive session start UTC — pass --session-start-utc.")
        return

    cd_model, sw_model = load_ocr(args.ocr_csv)
    if cd_model is None:
        print("ERROR: no usable countdown (cd_s) readings in OCR CSV — retune crops.")
        return
    cd_a, cd_b, cd_drop = cd_model
    print(f"countdown model: cd_s ≈ {cd_a:.4f}*v + {cd_b:.1f}  (slope ~ -1 expected; "
          f"{cd_drop} outliers dropped)")
    if sw_model:
        print(f"stopwatch model: sw_s ≈ {sw_model[0]:.4f}*v + {sw_model[1]:.1f}")

    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(args.obs)],
        stdout=subprocess.PIPE, text=True).stdout.strip()
    end = args.end if args.end is not None else (float(dur) if dur else 3600)

    sw_start = parse_utc(args.sw_start_utc) if args.sw_start_utc else None

    print(f"\n{'v(s)':>7} {'elapsed':>8} {'A=audio_utc':>14} {'data_utc':>14} "
          f"{'skew(s)':>9} {'ratio':>6}" + ("  bcast_lag(s)" if sw_start else ""))
    print("-" * (62 + (14 if sw_start else 0)))

    samples = []
    v = args.start
    while v < end - args.probe_dur:
        cd_s = cd_a * v + cd_b
        if args.abs_clock_tz is not None:
            # cd_s = seconds since LOCAL midnight; convert to UTC instant.
            midnight = sess_start.replace(hour=0, minute=0, second=0, microsecond=0)
            data_utc = midnight.timestamp() + cd_s - args.abs_clock_tz * 3600
            elapsed = data_utc - sess_start.timestamp()  # session-elapsed, for display
        else:
            elapsed = SESSION_MINUTES * 60 - cd_s
            data_utc = sess_start.timestamp() + elapsed
        target_combined = data_utc - aac_start.timestamp()  # prior: audio≈data

        probe = decode_probe(args.obs, v, args.probe_dur)
        if probe.size < PROBE_RATE:
            v += args.interval
            continue
        if args.aac:
            matched, ratio = correlate_in_file(
                args.aac, probe, PROBE_RATE, target_combined, args.search_window)
        else:
            matched, ratio = audio_sync.probe_offset_at(
                args.session, probe, PROBE_RATE, target_combined, window_s=args.search_window)
        if matched is None:
            v += args.interval
            continue
        a_utc = aac_start.timestamp() + matched
        skew = a_utc - data_utc
        flag = "" if ratio >= 3 else " (LOW)"
        line = (f"{v:7.0f} {elapsed:8.1f} "
                f"{datetime.fromtimestamp(a_utc, timezone.utc).strftime('%H:%M:%S'):>14} "
                f"{datetime.fromtimestamp(data_utc, timezone.utc).strftime('%H:%M:%S'):>14} "
                f"{skew:+9.2f} {ratio:6.1f}{flag}")
        if sw_start:
            w_utc = sw_start.timestamp() + (sw_model[0] * v + sw_model[1]) if sw_model else None
            if w_utc:
                line += f"  {w_utc - a_utc:+10.2f}"
        print(line)
        if ratio >= 3:
            samples.append((v, skew))
        v += args.interval

    if len(samples) >= 2:
        vs = np.array([s[0] for s in samples]); sk = np.array([s[1] for s in samples])
        slope, intercept = np.polyfit(vs, sk, 1)
        print("\n--- summary (confident windows only) ---")
        # skew = audio_broadcast_time - data_broadcast_time. skew<0 => the audio
        # content is OLDER than the data on screen => audio LAGS (is behind) data.
        print(f"mean audio-vs-data skew : {sk.mean():+.2f} s  "
              f"(audio {'LEADS' if sk.mean() > 0 else 'LAGS'} data by "
              f"{abs(sk.mean()):.1f}s)")
        print(f"drift                   : {slope * 60:+.2f} s per minute of recording "
              f"({'stable' if abs(slope * 60) < 0.05 else 'DRIFTING — rate mismatch'})")
        print(f"=> at t=0 skew {intercept:+.1f}s, growing to {intercept + slope * end:+.1f}s by end")
    else:
        print("\nNot enough confident windows — widen --search-window or check OCR.")


if __name__ == "__main__":
    main()
