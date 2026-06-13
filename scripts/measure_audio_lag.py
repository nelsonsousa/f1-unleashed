"""Measure the official-app audio lag vs our PDT-anchored commentary.

Experiment (per session):
  * The official F1 app plays the live commentary; its system audio is routed to
    a BlackHole device and recorded to a WAV (`--capture`), with the recorder's
    start instant noted in UTC (`--capture-start-utc`, from `date -u`).
  * Our app captures the same commentary into the session's `commentary.aac`,
    anchored to broadcast UTC via HLS PROGRAM-DATE-TIME (`audio_info.json`).

Both recordings carry the SAME commentary content, so cross-correlating a window
of the BlackHole capture against our aac finds where that content sits in our
(broadcast-UTC-anchored) stream. Comparing "when the official app emitted it"
(capture clock) with "when it aired per PDT" (our aac) gives the lag — purely
from content alignment, no by-ear guessing.

    official_emitted_utc = capture_start_utc + probe_offset_in_capture
    pdt_aired_utc        = aac_start_utc     + matched_offset_in_aac
    lag                  = official_emitted_utc − pdt_aired_utc
        (seconds the official app trails the broadcast PDT)

Usage:
    python scripts/measure_audio_lag.py \
        --capture /tmp/official_capture.wav \
        --capture-start-utc 2026-06-13T14:45:03.120Z \
        --session data/livetiming_cache/2026/<NN>_Barcelona/<id>_Practice_2 \
        [--probe-offset 900 --probe-dur 90] \
        [--t0-local 2026-06-13T16:45:10 --session-start-utc 2026-06-13T15:00:00Z]

The optional --t0-local / --session-start-utc pair is a cross-check: the instant
you marked the official session clock starting, and the scheduled start in UTC
(17:00 Amsterdam = 15:00:00Z). It validates that the machine clock used for
--capture-start-utc agrees with the session-start anchor.
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import audio_sync

PROBE_RATE = 8000


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def aac_byte0_anchor(session_path: Path) -> datetime:
    """Broadcast-UTC of the combined stream's byte 0 = the EARLIEST segment's
    PDT start_utc (probe_offset_at correlates against rotated+current in order)."""
    _, info_file = audio_sync._earliest_segment(session_path)
    import json
    info = json.loads(Path(info_file).read_text())
    return parse_utc(info["start_utc"])


def decode_probe(capture: Path, offset_s: float, dur_s: float) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{offset_s:.3f}", "-t", f"{dur_s:.3f}", "-i", str(capture),
         "-ac", "1", "-ar", str(PROBE_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120,
    )
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, type=Path)
    ap.add_argument("--capture-start-utc", required=True)
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--probe-offset", type=float, default=900.0,
                    help="seconds into the capture to take the probe window (default 900 = 15min, past pre-session)")
    ap.add_argument("--probe-dur", type=float, default=90.0)
    ap.add_argument("--assumed-lag", type=float, default=30.0,
                    help="rough guess of the lag, to centre the search window")
    ap.add_argument("--search-window", type=float, default=900.0,
                    help="± seconds searched around the guess (default 15min)")
    ap.add_argument("--t0-local")
    ap.add_argument("--session-start-utc")
    args = ap.parse_args()

    cap_start = parse_utc(args.capture_start_utc)
    aac_start = aac_byte0_anchor(args.session)

    segs = audio_sync._ordered_audio_segments(args.session) if hasattr(
        audio_sync, "_ordered_audio_segments") else []
    n_seg = len(list(args.session.glob("commentary.[0-9][0-9][0-9].aac"))) + \
        (1 if (args.session / "commentary.aac").exists() else 0)
    if n_seg > 1:
        print(f"WARNING: {n_seg} audio segments — inter-segment capture gaps "
              f"(issue I15) can skew the combined-stream offset. Prefer a "
              f"single-segment capture.")

    probe = decode_probe(args.capture, args.probe_offset, args.probe_dur)
    if probe.size < PROBE_RATE:
        print("ERROR: probe window decoded empty/too short — check --capture / --probe-offset.")
        return

    official_emitted = cap_start.timestamp() + args.probe_offset       # UTC secs
    # Centre the aac search on (emitted − assumed_lag) relative to aac byte0.
    target_combined = official_emitted - args.assumed_lag - aac_start.timestamp()

    matched, ratio = audio_sync.probe_offset_at(
        args.session, probe, PROBE_RATE, target_combined, window_s=args.search_window)
    if matched is None:
        print("ERROR: could not decode the aac search window (file missing/empty?).")
        return

    pdt_aired = aac_start.timestamp() + matched
    lag = official_emitted - pdt_aired

    print(f"correlation confidence ratio : {ratio:.1f}  "
          f"({'OK' if ratio >= 3 else 'LOW — treat as unreliable'})")
    print(f"official emitted (capture)   : {datetime.fromtimestamp(official_emitted, timezone.utc).strftime('%H:%M:%S.%f')[:-3]}Z")
    print(f"pdt aired (our aac)          : {datetime.fromtimestamp(pdt_aired, timezone.utc).strftime('%H:%M:%S.%f')[:-3]}Z  (combined offset {matched:.2f}s)")
    print(f"LAG official-vs-broadcast    : {lag:+.2f} s  "
          f"(official app emits this audio {lag:.1f}s after its PDT air time)")

    if args.t0_local and args.session_start_utc:
        t0 = parse_utc(args.t0_local + ("Z" if "Z" not in args.t0_local and "+" not in args.t0_local else ""))
        ss = parse_utc(args.session_start_utc)
        skew = (t0 - ss).total_seconds()
        print(f"\ncross-check: marked session-start local {t0.strftime('%H:%M:%S')} vs "
              f"scheduled {ss.strftime('%H:%M:%S')}Z → machine/clock skew {skew:+.1f}s "
              f"(should be ~0 if clock is NTP-synced; subtract from lag if not)")


if __name__ == "__main__":
    main()
