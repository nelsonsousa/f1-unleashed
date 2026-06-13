"""Investigation dump: NOR (car 1), Melbourne FP3.

Emits, sorted by message timestamp, these message types:
  1 NumberOfLaps        (raw TimingData, envelope DateTime)
  2 LapTime             (raw TimingData LastLapTime.Value, envelope DateTime)
  3 InPit=true          (raw TimingData, transition false->true)
  4 PitOut=true         (raw TimingData, transition false->true)
  5 SF_crossing(0%)     (processed position payload ts: 1st sample with dp<10
                         after a dp>90 sample — the 0.x% sample used to
                         interpolate the lap's first telemetry sample)
  6 last_pos_pre_InPit  (last position payload ts before each InPit=true)
  7 first_pos_post_PitOut (first position payload ts after each PitOut=true)

Timestamps: hh:MM:ss.SSS (UTC, original).
"""
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESS = ROOT / "data/livetiming_cache/2026/1279_Melbourne/11229_Practice_3"
DB = ROOT / "tmp/2026_1279_Melbourne_11229_Practice_3.db"
OUT = ROOT / "tmp/nor_fp3_messages.csv"
CAR = "1"
WRAP_HIGH, WRAP_LOW = 90.0, 10.0


def fmt(dt):
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


# ── raw TimingData events ───────────────────────────────────────────────────
def read_timing():
    rows = []                       # (dt, type, detail)
    prev_inpit = prev_pitout = False
    for line in (SESS / "live.jsonl").open():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("Type") != "TimingData":
            continue
        lines = (m.get("Json") or {}).get("Lines") if isinstance(m.get("Json"), dict) else None
        if not isinstance(lines, dict) or CAR not in lines:
            continue
        d = lines[CAR]
        if not isinstance(d, dict):
            continue
        dt = datetime.fromisoformat(m["DateTime"])
        if "NumberOfLaps" in d:
            rows.append((dt, "NumberOfLaps", d["NumberOfLaps"]))
        llt = d.get("LastLapTime")
        if isinstance(llt, dict) and llt.get("Value"):
            rows.append((dt, "LapTime", llt["Value"]))
        if "InPit" in d:
            v = bool(d["InPit"])
            if v and not prev_inpit:
                rows.append((dt, "InPit=true", ""))
            prev_inpit = v
        if "PitOut" in d:
            v = bool(d["PitOut"])
            if v and not prev_pitout:
                rows.append((dt, "PitOut=true", ""))
            prev_pitout = v
    return rows


# ── processed position stream for car 1 ─────────────────────────────────────
def read_positions():
    """-> [(dt, dp)] in chronological order (payload timestamp)."""
    con = sqlite3.connect(DB)
    # session date from the first message wall_clock is only time-of-day; take
    # the date from a raw envelope so we can build full datetimes.
    date0 = None
    for line in (SESS / "live.jsonl").open():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("DateTime"):
            date0 = datetime.fromisoformat(m["DateTime"]).date()
            break
    out = []
    for wc, data in con.execute(
            "SELECT wall_clock, data FROM messages WHERE topic='position' ORDER BY offset_ms"):
        dd = json.loads(data)
        if CAR not in dd:
            continue
        coords = dd[CAR]
        if not isinstance(coords, list) or len(coords) < 3:
            continue
        h, mn, rest = wc.split(":")
        s, ms = (rest.split(".") + ["0"])[:2]
        dt = datetime(date0.year, date0.month, date0.day, int(h), int(mn), int(s),
                      int(ms.ljust(3, "0")[:3]) * 1000, tzinfo=timezone.utc)
        out.append((dt, float(coords[2])))
    con.close()
    return out


def main():
    timing = read_timing()
    pos = read_positions()
    rows = list(timing)

    # 5: S/F crossings — first dp<WRAP_LOW after dp>WRAP_HIGH
    for i in range(1, len(pos)):
        pdp = pos[i - 1][1]
        dp = pos[i][1]
        if pdp > WRAP_HIGH and dp < WRAP_LOW:
            rows.append((pos[i][0], "SF_crossing(0%)", f"dp={dp:.3f} (prev {pdp:.3f})"))

    pos_dts = [p[0] for p in pos]
    import bisect
    # 6: last position before each InPit=true
    for dt, typ, _ in timing:
        if typ == "InPit=true":
            j = bisect.bisect_left(pos_dts, dt) - 1
            if j >= 0:
                rows.append((pos[j][0], "last_pos_pre_InPit", f"dp={pos[j][1]:.3f}"))
    # 7: first position after each PitOut=true
    for dt, typ, _ in timing:
        if typ == "PitOut=true":
            j = bisect.bisect_right(pos_dts, dt)
            if j < len(pos):
                rows.append((pos[j][0], "first_pos_post_PitOut", f"dp={pos[j][1]:.3f}"))

    rows.sort(key=lambda r: r[0])
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "type", "detail"])
        for dt, typ, detail in rows:
            w.writerow([fmt(dt), typ, detail])
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
