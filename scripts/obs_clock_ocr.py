"""OCR the on-screen clocks from the OBS screen-capture recording.

The recording (official F1 app + macOS Stopwatch + session countdown, all in
one frame) carries two clocks we read per sampled frame:

  * macOS Stopwatch  — "MM:SS,CC" big white digits. Anchored to wall time:
                       stopwatch 00:00 == a known laptop-clock instant.
  * Session countdown — "MM:SS" at the bottom of the F1 page, counting DOWN
                       from 60:00. session-elapsed = 60:00 - displayed.

Output: CSV (video_time_s, sw_raw, sw_s, cd_raw, cd_s) for obs_sync_analyze.py.

Crop boxes are "x,y,w,h" in pixels of the recorded frame. Resolution isn't
known ahead of time, so first find them:

    python scripts/obs_clock_ocr.py --obs REC.mkv --dump 60 --dump-out tmp/frame.png
    # open tmp/frame.png, read off the stopwatch + countdown pixel boxes, then:
    python scripts/obs_clock_ocr.py --obs REC.mkv \
        --sw-box 540,690,680,180 --cd-box 250,930,260,70 \
        --interval 15 --out tmp/obs_clocks.csv
"""
import argparse
import csv
import re
from pathlib import Path

import cv2
import numpy as np
import pytesseract


def _box(s):
    x, y, w, h = (int(v) for v in s.split(","))
    return x, y, w, h


def _ocr_digits(img, allow_comma):
    """OCR a cropped clock region. Tries both threshold polarities (light-on-
    dark and dark-on-light) and returns the first plausible digit string."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    whitelist = "0123456789:," if allow_comma else "0123456789:"
    cfg = f"--psm 7 -c tessedit_char_whitelist={whitelist}"
    best = ""
    for inv in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, th = cv2.threshold(gray, 0, 255, inv | cv2.THRESH_OTSU)
        txt = pytesseract.image_to_string(th, config=cfg).strip()
        digits = re.sub(r"[^0-9:,]", "", txt)
        if len(re.sub(r"[^0-9]", "", digits)) >= len(re.sub(r"[^0-9]", "", best)):
            best = digits
    return best


def _parse_clock(raw):
    """Parse a clock string to seconds (float), handling all three on-screen
    formats: 'H:MM:SS' (P3 session countdown), 'MM:SS,CC' (macOS stopwatch),
    and plain 'MM:SS'. Returns None if unparseable."""
    if not raw:
        return None
    m = re.search(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,2}))?", raw)
    if not m:
        return None
    h = int(m.group(1)) if m.group(1) else 0
    mm, ss = int(m.group(2)), int(m.group(3))
    val = h * 3600 + mm * 60 + ss
    if m.group(4):
        val += int(m.group(4)) / (10 ** len(m.group(4)))
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", required=True, type=Path)
    ap.add_argument("--dump", type=float, help="dump one full frame at this video-time (s) and exit")
    ap.add_argument("--dump-out", type=Path, default=Path("tmp/obs_frame.png"))
    ap.add_argument("--sw-box", type=_box, help="stopwatch crop x,y,w,h")
    ap.add_argument("--cd-box", type=_box, help="countdown crop x,y,w,h")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--out", type=Path, default=Path("tmp/obs_clocks.csv"))
    args = ap.parse_args()

    cap = cv2.VideoCapture(str(args.obs))
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.obs}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    dur = nframes / fps if nframes else 0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"{args.obs.name}: {w}x{h} @ {fps:.2f}fps, {dur/60:.1f} min")

    def frame_at(t):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, fr = cap.read()
        return fr if ok else None

    if args.dump is not None:
        fr = frame_at(args.dump)
        if fr is None:
            print("ERROR: no frame at that time")
            return
        args.dump_out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.dump_out), fr)
        print(f"wrote {args.dump_out} ({w}x{h}). Read off the sw/cd boxes (x,y,w,h).")
        return

    if not args.sw_box and not args.cd_box:
        print("ERROR: pass --sw-box and/or --cd-box (use --dump first to find them).")
        return

    end = args.end if args.end is not None else dur
    rows = []
    t = args.start
    while t < end:
        fr = frame_at(t)
        if fr is None:
            break
        sw_raw = cd_raw = ""
        if args.sw_box:
            x, y, bw, bh = args.sw_box
            sw_raw = _ocr_digits(fr[y:y + bh, x:x + bw], allow_comma=True)
        if args.cd_box:
            x, y, bw, bh = args.cd_box
            cd_raw = _ocr_digits(fr[y:y + bh, x:x + bw], allow_comma=False)
        rows.append([round(t, 3), sw_raw, _parse_clock(sw_raw), cd_raw, _parse_clock(cd_raw)])
        t += args.interval
    cap.release()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["video_time_s", "sw_raw", "sw_s", "cd_raw", "cd_s"])
        wr.writerows(rows)
    ok = sum(1 for r in rows if r[2] is not None or r[4] is not None)
    print(f"wrote {len(rows)} rows ({ok} parsed) -> {args.out}")
    print("Spot-check a few rows; if OCR is noisy, retune the crop boxes via --dump.")


if __name__ == "__main__":
    main()
