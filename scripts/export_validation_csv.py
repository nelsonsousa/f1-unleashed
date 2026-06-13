"""Export a per-(event, session, driver, lap) CSV joining authoritative lap
timing (NoL) with telemetry-lap data, for manual discrepancy matching.

Columns:
  event, session, quali_part, car, tla, lap, in_lap, out_lap,
  nol_ts, lap_time, tele_start_ts, tele_end_ts, tele_samples, tele_duration

Timestamps  -> hh:MM:ss.SSS (UTC, as recorded)
Durations   -> MM:ss.SSS

Sources:
  * raw live.jsonl  -> NoL value + the NoL message timestamp (envelope DateTime),
    InPit/PitOut transitions (in/out lap flags), per lap.
  * processed DB    -> driverLaps lastLap (lap_time per lap), telemetryLap:{d}:{n}
    (samples + absolute end wall_clock), qualifyingSegment (Q-part timeline),
    driverList (TLA).
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.parse_official import parse_lapseries, parse_classification

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data/livetiming_cache/2026"
TMP = ROOT / "tmp"
OFF = TMP / "official_results"
OUT = TMP / "validation_export.csv"

# event dir suffix -> (round, country code)
EVENT_CODE = {
    "Melbourne": ("01", "aus"), "Shanghai": ("02", "chn"), "Suzuka": ("03", "jpn"),
    "Miami_Gardens": ("04", "usa"), "Montréal": ("05", "can"), "Monte_Carlo": ("06", "mon"),
}
# session dir suffix -> (session code, classification doctype, lapseries doctype)
SESSION_CODE = {
    "Practice_1": ("p1", "firstpracticesessionclassification", "firstpracticesessionlaptimes"),
    "Practice_2": ("p2", "secondpracticesessionclassification", "secondpracticesessionlaptimes"),
    "Practice_3": ("p3", "thirdpracticesessionclassification", "thirdpracticesessionlaptimes"),
    "Qualifying": ("q0", "qualifyingsessionprovisionalclassification", "qualifyingsessionlaptimes"),
    "Race": ("r0", "raceprovisionalclassification", "racelapanalysis"),
    "Sprint_Qualifying": ("sq0", "sprintqualifyingsessionprovisionalclassification", "sprintqualifyingsessionlaptimes"),
    "Sprint": ("s0", "sprintprovisionalclassification", "sprintlapanalysis"),
}


def _find_pdf(rnd, ccc, scode, doctype):
    hits = sorted(OFF.glob(f"2026_{rnd}_{ccc}_f1_{scode}_timing_{doctype}*.pdf"))
    return hits[0] if hits else None


def load_official(session_dir):
    """-> (lapseries {car:{lap:(time,is_pit)}}, classification {car:laps|{seg:laps}})."""
    ev = event_name(session_dir.parent.name)
    sk = session_dir.name.split("_", 1)[1]
    if ev not in EVENT_CODE or sk not in SESSION_CODE:
        return {}, {}
    rnd, ccc = EVENT_CODE[ev]
    scode, class_dt, laps_dt = SESSION_CODE[sk]
    lap_pdf = _find_pdf(rnd, ccc, scode, laps_dt)
    cls_pdf = _find_pdf(rnd, ccc, scode, class_dt)
    lapseries = {}
    if lap_pdf:
        for car, seq in parse_lapseries(lap_pdf).items():
            lapseries[car] = {ln: (t, p) for ln, t, p in seq}
    classification = parse_classification(cls_pdf) if cls_pdf else {}
    return lapseries, classification


def fmt_off_time(s):
    """Official time string -> duration MM:ss.SSS, or verbatim clock (hh:MM:ss)."""
    if not s:
        return ""
    return norm_laptime(s) if "." in s else s

SESSION_NAMES = {
    "Practice_1": "Practice 1", "Practice_2": "Practice 2", "Practice_3": "Practice 3",
    "Qualifying": "Qualifying", "Race": "Race",
    "Sprint_Qualifying": "Sprint Qualifying", "Sprint": "Sprint",
}


def event_name(dirname):           # 1279_Melbourne -> Melbourne
    return dirname.split("_", 1)[1]


def session_name(dirname):         # 11230_Qualifying -> Qualifying
    key = dirname.split("_", 1)[1]
    return SESSION_NAMES.get(key, key.replace("_", " "))


def db_path_for(session_dir):
    return TMP / f"2026_{session_dir.parent.name}_{session_dir.name}.db"


def fmt_ts(dt):
    """datetime -> hh:MM:ss.SSS (UTC)."""
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def fmt_dur(ms):
    """milliseconds -> MM:ss.SSS."""
    if ms is None:
        return ""
    ms = int(round(ms))
    m, rem = divmod(ms, 60000)
    s, mmm = divmod(rem, 1000)
    return f"{m:02d}:{s:02d}.{mmm:03d}"


def norm_laptime(s):
    """'1:38.007' (M:SS.mmm) -> '01:38.007' (MM:ss.SSS)."""
    if not s or ":" not in s:
        return s or ""
    mm, rest = s.split(":", 1)
    return f"{int(mm):02d}:{rest}"


def wc_to_ms(wc):
    """'HH:MM:SS.mmm' -> ms since 00:00."""
    h, m, rest = wc.split(":")
    s, mmm = (rest.split(".") + ["0"])[:2]
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(mmm.ljust(3, "0")[:3])


def ms_to_wc(ms):
    ms %= 86400_000
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60000)
    s, mmm = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{mmm:03d}"


# ── raw: NoL ts + in/out lap flags ──────────────────────────────────────────
def read_raw(session_dir):
    nol_ts = defaultdict(dict)     # car -> {lap: datetime}
    in_lap = defaultdict(set)
    out_lap = defaultdict(set)
    cur = {}                       # car -> current NoL
    prev_inpit = {}
    prev_pitout = {}
    for line in (session_dir / "live.jsonl").open():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("Type") != "TimingData":
            continue
        dt = m.get("DateTime")
        lines = (m.get("Json") or {}).get("Lines") if isinstance(m.get("Json"), dict) else None
        if not isinstance(lines, dict):
            continue
        for car, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "NumberOfLaps" in d:
                n = d["NumberOfLaps"]
                cur[car] = n
                if n not in nol_ts[car]:
                    nol_ts[car][n] = datetime.fromisoformat(dt)
            c = cur.get(car)
            if "InPit" in d:
                v = bool(d["InPit"])
                if v and not prev_inpit.get(car) and c is not None:
                    in_lap[car].add(c)
                prev_inpit[car] = v
            if "PitOut" in d:
                v = bool(d["PitOut"])
                if v and not prev_pitout.get(car) and c is not None:
                    out_lap[car].add(c)
                prev_pitout[car] = v
    return nol_ts, in_lap, out_lap


# ── processed DB ────────────────────────────────────────────────────────────
def read_db(db):
    con = sqlite3.connect(db)
    tla = {}
    for (data,) in con.execute("SELECT data FROM messages WHERE topic='driverList' LIMIT 1"):
        for car, info in json.loads(data).items():
            tla[car] = info.get("tla") or car
    laptime = defaultdict(dict)     # car -> {lap: time_str}
    tele = defaultdict(dict)        # car -> {lap: (end_wc, samples)}
    for topic, wc, data in con.execute(
            "SELECT topic, wall_clock, data FROM messages "
            "WHERE topic LIKE 'driverLaps:%' OR topic LIKE 'telemetryLap:%' ORDER BY offset_ms"):
        if topic.startswith("driverLaps:"):
            car = topic.split(":", 1)[1]
            ll = json.loads(data).get("lastLap")
            if isinstance(ll, dict) and ll.get("lap") is not None and ll.get("time"):
                laptime[car][int(ll["lap"])] = ll["time"]
        else:
            _, car, lap = topic.split(":")
            tele[car][int(lap)] = (wc, json.loads(data))
    # qualifying segment timeline: [(ms_offset, segment)]
    seg_tl = []
    for off, data in con.execute(
            "SELECT offset_ms, data FROM messages WHERE topic='qualifyingSegment' ORDER BY offset_ms"):
        seg = json.loads(data).get("segment")
        if seg and (not seg_tl or seg_tl[-1][1] != seg):
            seg_tl.append((off, seg))
    # session start wall_clock (offset 0) to map lap ts -> offset for segment lookup
    start_wc = None
    row = con.execute("SELECT wall_clock FROM messages ORDER BY offset_ms LIMIT 1").fetchone()
    if row:
        start_wc = row[0]
    con.close()
    return tla, laptime, tele, seg_tl, start_wc


def seg_at_offset(seg_tl, off):
    seg = ""
    for o, s in seg_tl:
        if o <= off:
            seg = s
        else:
            break
    return seg


def main():
    rows = []
    sessions = []
    import time
    now = time.time()
    for event in sorted(CACHE.iterdir()):
        if event.is_dir():
            for s in sorted(event.iterdir()):
                jsonl = s / "live.jsonl"
                if not jsonl.exists():
                    continue
                if now - jsonl.stat().st_mtime < 3600:   # live capture in progress → skip
                    print(f"skip (live): {s.parent.name}/{s.name}")
                    continue
                sessions.append(s)

    for sdir in sessions:
        db = db_path_for(sdir)
        if not db.exists():
            continue
        ev = event_name(sdir.parent.name)
        sess = session_name(sdir.name)
        is_quali = "Qualifying" in sdir.name or sess in ("Qualifying", "Sprint Qualifying")
        nol_ts, in_lap, out_lap = read_raw(sdir)
        tla, laptime, tele, seg_tl, start_wc = read_db(db)
        lapseries, classification = load_official(sdir)
        start_ms = wc_to_ms(start_wc) if start_wc else 0

        cars = sorted(set(nol_ts) | set(laptime) | set(tele),
                      key=lambda c: int(c) if c.isdigit() else 999)
        srows = []          # (row_list, car, group_key) for class-laps post-pass
        for car in cars:
            laps_present = set(nol_ts.get(car, {})) | set(laptime.get(car, {})) | set(tele.get(car, {}))
            for lap in sorted(laps_present):
                ndt = nol_ts.get(car, {}).get(lap)
                qpart = ""
                if is_quali and ndt is not None and seg_tl:
                    off = wc_to_ms(fmt_ts(ndt)) - start_ms
                    qpart = seg_at_offset(seg_tl, off)
                # telemetry lap
                ts_start = ts_end = tdur = ""
                nsamp = ""
                if lap in tele.get(car, {}):
                    end_wc, samples = tele[car][lap]
                    nsamp = len(samples)
                    if samples:
                        tvals = [s[6] for s in samples]
                        dur = max(tvals) - min(tvals)
                        tdur = fmt_dur(dur)
                        ts_end = end_wc
                        ts_start = ms_to_wc(wc_to_ms(end_wc) - dur)
                # official lap (aligned by crossing number) + lap time
                off_lap = off_lt = ""
                if lap in lapseries.get(car, {}):
                    off_lap = lap
                    off_lt = fmt_off_time(lapseries[car][lap][0])
                row = [
                    ev, sess, qpart, car, tla.get(car, car), lap,
                    "Y" if lap in in_lap.get(car, set()) else "",
                    "Y" if lap in out_lap.get(car, set()) else "",
                    fmt_ts(ndt) if ndt else "",
                    norm_laptime(laptime.get(car, {}).get(lap, "")),
                    ts_start, ts_end, nsamp, tdur,
                    off_lap, off_lt, "",          # off_class_laps filled in post-pass
                ]
                srows.append((row, car, qpart if is_quali else ""))

        # classification LAPS on each driver's last lap (per Q-part in quali).
        # "last lap" = highest lap that actually has a NoL timestamp (row[8]),
        # so a phantom telemetry-only lap doesn't capture the value.
        last_idx = {}                              # (car, group) -> srows index of max NoL lap
        for i, (row, car, grp) in enumerate(srows):
            if not row[8]:                         # no NoL ts -> not a real lap
                continue
            k = (car, grp)
            if k not in last_idx or row[5] > srows[last_idx[k]][0][5]:
                last_idx[k] = i
        for (car, grp), i in last_idx.items():
            cl = classification.get(car)
            if isinstance(cl, dict):              # quali: per-segment
                val = cl.get(grp)
            else:                                  # practice/race: single
                val = cl
            if val is not None:
                srows[i][0][16] = val

        rows.extend(r for r, _, _ in srows)

    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "session", "quali_part", "car", "tla", "lap",
                    "in_lap", "out_lap", "nol_ts", "lap_time",
                    "tele_start_ts", "tele_end_ts", "tele_samples", "tele_duration",
                    "off_lap", "off_lap_time", "off_class_laps"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
