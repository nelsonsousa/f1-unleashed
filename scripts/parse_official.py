"""Parse FIA official timing PDFs in tmp/official_results.

Three document families:
  * classification (practice/sprint/race): single LAPS column -> {car: laps}
  * classification (qualifying/sprint-qualifying): per-segment LAPS columns
        -> {car: {"Q1": n, "Q2": n, "Q3": n}}  (or SQ1/2/3)
  * lap times (non-race) / lap analysis (race): multi-column per-driver crossing
        list -> {car: [(lap_no, time_str, is_pit)]}
        (lap 1 time is a clock HH:MM:SS; others are lap durations; 'P' = pit-lane crossing)
"""
import re
import sys
from pathlib import Path

import pdfplumber

OFF = Path(__file__).resolve().parent.parent / "tmp/official_results"

TIME_RE = re.compile(r"^\d?\d:\d\d[.:]\d\d\d?$|^\d?\d:\d\d:\d\d$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]+$")


def _lines(words, ytol=3):
    """Group words into rows by `top`."""
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(w["top"] - rows[-1][0]) <= ytol:
            rows[-1][1].append(w)
        else:
            rows.append((w["top"], [w]))
    return [(t, sorted(ws, key=lambda w: w["x0"])) for t, ws in rows]


FIRSTNAME_RE = re.compile(r"^[A-Z][a-z]{2,}$")   # "Lando","Max" — not "NO"/"TIME"/"NORRIS"


def _is_header(ws):
    return any(FIRSTNAME_RE.match(w["text"]) for w in ws)


def _driver_blocks(ws):
    """Valid driver blocks in a header row: int(1-99) + Title-case first name +
    UPPERCASE surname. Excludes title/footer artefacts ('1 QATAR', 'Page 1 of')."""
    blocks = []
    for j, w in enumerate(ws):
        if (w["text"].isdigit() and 1 <= int(w["text"]) <= 99
                and j + 2 < len(ws)
                and FIRSTNAME_RE.match(ws[j + 1]["text"])
                and ws[j + 2]["text"].isupper() and len(ws[j + 2]["text"]) > 2):
            blocks.append((w["text"], w["x0"]))
    return blocks


def parse_lapseries(pdf_path):
    """{car: [(lap_no, time_str, is_pit)]} from a lap-times / lap-analysis PDF."""
    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1.5)
            rows = _lines(words)
            # real driver headers only (rows yielding >=1 valid driver block)
            heads = [(t, _driver_blocks(ws)) for t, ws in rows]
            heads = [(t, b) for t, b in heads if b]
            for hi, (top, blocks) in enumerate(heads):
                xs = [x for _, x in blocks] + [page.width + 1]
                band_bottom = heads[hi + 1][0] if hi + 1 < len(heads) else 1e9
                for bi, (car, lx) in enumerate(blocks):
                    rx = xs[bi + 1] - 2
                    toks = []
                    for t, rws in rows:
                        if t <= top + 4 or t >= band_bottom:
                            continue
                        for w in rws:
                            if lx - 3 <= w["x0"] < rx:
                                toks.append(w["text"])
                    out.setdefault(car, [])
                    _pair(toks, out[car])
    return out


def _pair(toks, acc):
    """Linear scan: int (lap) -> optional 'P' -> time string."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.isdigit():
            lap = int(t)
            is_pit = False
            j = i + 1
            if j < len(toks) and toks[j] == "P":
                is_pit = True
                j += 1
            if j < len(toks) and TIME_RE.match(toks[j]):
                acc.append((lap, toks[j], is_pit))
                i = j + 1
                continue
        i += 1


SEG_RE = re.compile(r"^S?Q[123]$")


def _cx(w):
    return (w["x0"] + w["x1"]) / 2


def parse_classification(pdf_path):
    """Practice/sprint/race -> {car: laps:int}.
    Qualifying/sprint-qualifying -> {car: {seg_label: laps:int}}.

    Reads the integer under each 'LAPS' header column by x-position, so it is
    immune to per-document column order (practice has TIME before LAPS, race has
    LAPS before TIME, quali has three LAPS columns)."""
    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows = _lines(page.extract_words(x_tolerance=1.5))
            # header row = the one containing 'LAPS'
            hdr = next(((t, ws) for t, ws in rows
                        if any(w["text"] == "LAPS" for w in ws)), None)
            if not hdr:
                continue
            _, hws = hdr
            laps_x = [_cx(w) for w in hws if w["text"] == "LAPS"]
            seg_labels = [w["text"] for w in hws if SEG_RE.match(w["text"])]
            is_seg = len(laps_x) > 1
            for t, ws in rows:
                # driver row: pos, car, Title-case first name, ...
                if len(ws) < 3 or not ws[0]["text"].isdigit() \
                        or not ws[1]["text"].isdigit() \
                        or not FIRSTNAME_RE.match(ws[2]["text"]):
                    continue
                car = ws[1]["text"]
                ints = [(w, int(w["text"])) for w in ws
                        if w["text"].isdigit()][2:]  # skip pos, car
                vals = []
                for lx in laps_x:
                    near = [(abs(_cx(w) - lx), v) for w, v in ints if abs(_cx(w) - lx) < 25]
                    vals.append(min(near)[1] if near else None)
                if is_seg:
                    labels = seg_labels if len(seg_labels) == len(vals) else \
                        [f"Q{i+1}" for i in range(len(vals))]
                    out[car] = {labels[i]: vals[i] for i in range(len(vals)) if vals[i] is not None}
                else:
                    out[car] = vals[0] if vals else None
    return out


if __name__ == "__main__":
    p = OFF / sys.argv[1]
    if "laptimes" in p.name or "lapanalysis" in p.name:
        r = parse_lapseries(p)
        for car in sorted(r, key=lambda c: int(c)):
            laps = r[car]
            if not laps:
                print(f"car {car:>3}: (no crossings)")
                continue
            pits = sum(1 for _, _, pit in laps if pit)
            print(f"car {car:>3}: {len(laps):3d} crossings (max lap {max(l for l,_,_ in laps)}), {pits} pit")
    else:
        r = parse_classification(p)
        for car in sorted(r, key=lambda c: int(c)):
            print(f"car {car:>3}: {r[car]}")
