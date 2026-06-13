"""Continuous delay monitor for the FP2 audio/data-sync experiment.

Every INTERVAL seconds, sample and journal:
  * audio_lag_s    : official-app audio vs broadcast PDT (cross-correlate a recent
                     window of the BlackHole capture against our commentary.aac)
  * corr_ratio     : correlation confidence (>~5 = trustworthy)
  * hls_edge_lag_s : our audio source freshness vs real (poll-wall − newest PDT
                     from pdt_map.jsonl)
  * data_edge_lag_s: our data source freshness vs real (now − newest live.jsonl
                     envelope DateTime)
  * wav_dur_s / wav_grew : capture health

Appends one JSON record per tick to tmp/fp2_delay_journal.jsonl and prints a
human line. Self-terminates when live.jsonl goes stale (session ended) or after
MAX_MIN. Robust: a failed metric is logged as null, the loop continues.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services import audio_sync

CAP = Path("/tmp/official_capture.wav")
ANCHOR_FILE = Path("/tmp/blackhole_start_utc.txt")
SESSION = ROOT / "data/livetiming_cache/2026/1287_Barcelona/11301_Practice_2"
JOURNAL = ROOT / "tmp/fp2_delay_journal.jsonl"
INTERVAL = 60
PROBE_DUR = 90.0
PROBE_BACKS = [150.0, 260.0, 400.0]   # candidate windows (s before wav edge); keep highest-confidence
RATE = 8000
BYTES_PER_S = RATE * 2      # s16le mono
MAX_MIN = 100
STALE_S = 300               # live.jsonl idle this long → session over


def parse_utc(s):
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def wav_seconds():
    return max(0.0, (CAP.stat().st_size - 44) / BYTES_PER_S)


def aac_start_epoch():
    _, info = audio_sync._earliest_segment(SESSION)
    return parse_utc(json.loads(Path(info).read_text())["start_utc"])


def decode_probe(off, dur):
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", str(CAP),
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90,
    )
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)


def audio_lag(cap_start):
    """Try several recent windows; return the highest-confidence lag.
    -> (lag_s, ratio, wav_dur, probe_off)."""
    wav_dur = wav_seconds()
    aac0 = aac_start_epoch()
    best = (None, 0.0, None)   # lag, ratio, off
    for back in PROBE_BACKS:
        off = wav_dur - back
        if off < 5.0:
            continue
        probe = decode_probe(off, PROBE_DUR)
        if probe.size < RATE:
            continue
        emitted = cap_start + off
        target = emitted - 30.0 - aac0
        matched, ratio = audio_sync.probe_offset_at(SESSION, probe, RATE, target, window_s=300.0)
        if matched is not None and ratio > best[1]:
            best = (emitted - (aac0 + matched), ratio, off)
    return best[0], best[1], wav_dur, best[2]


def last_line(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 8192))
        return f.read().decode("utf-8", "replace").splitlines()[-1]


def hls_edge_lag():
    r = json.loads(last_line(SESSION / "pdt_map.jsonl"))
    wall = r["wall_ms"] / 1000.0
    edge = parse_utc(r["edge_pdt_utc"])
    return wall - edge


def data_edge_lag():
    # newest envelope DateTime in live.jsonl vs now
    dt = None
    with open(SESSION / "live.jsonl", "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 16384))
        for line in reversed(f.read().decode("utf-8", "replace").splitlines()):
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("DateTime"):
                dt = parse_utc(m["DateTime"])
                break
    return (time.time() - dt) if dt else None


def main():
    cap_start = parse_utc(ANCHOR_FILE.read_text())
    print(f"monitor start; capture anchor {ANCHOR_FILE.read_text().strip()}", flush=True)
    t0 = time.time()
    prev_size = -1
    while time.time() - t0 < MAX_MIN * 60:
        rec = {"wall_utc": datetime.now(timezone.utc).strftime("%H:%M:%S")}
        try:
            lag, ratio, wav_dur, probe_off = audio_lag(cap_start)
            rec["audio_lag_s"] = round(lag, 2) if lag is not None else None
            rec["corr_ratio"] = round(ratio, 1)
            rec["wav_dur_s"] = round(wav_dur, 1)
            rec["probe_off_s"] = round(probe_off, 1) if probe_off is not None else None
        except Exception as e:  # noqa: BLE001
            rec["audio_lag_s"] = None; rec["corr_ratio"] = 0.0
            rec["err_audio"] = str(e)[:80]
        for name, fn in (("hls_edge_lag_s", hls_edge_lag), ("data_edge_lag_s", data_edge_lag)):
            try:
                v = fn(); rec[name] = round(v, 1) if v is not None else None
            except Exception:  # noqa: BLE001
                rec[name] = None
        sz = CAP.stat().st_size if CAP.exists() else 0
        rec["wav_grew"] = sz > prev_size
        prev_size = sz

        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{rec['wall_utc']}  audio_lag={rec.get('audio_lag_s')}s "
              f"(r={rec.get('corr_ratio')})  hls_edge={rec.get('hls_edge_lag_s')}s  "
              f"data_edge={rec.get('data_edge_lag_s')}s  wav_grew={rec['wav_grew']}", flush=True)

        # session-over check
        try:
            if time.time() - (SESSION / "live.jsonl").stat().st_mtime > STALE_S:
                print("live.jsonl stale → session over, stopping monitor", flush=True)
                break
        except OSError:
            pass
        time.sleep(INTERVAL)
    print("monitor done", flush=True)


if __name__ == "__main__":
    main()
