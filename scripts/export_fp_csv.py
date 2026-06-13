"""Per-(driver, lap) CSV for Barcelona FP1 + FP2 — lap timing + telemetry only
(no official results). Reuses the read helpers from export_validation_csv.

Columns: event, session, quali_part, car, tla, lap, out_lap, in_lap, nol_ts,
lap_time, tele_start_ts, tele_end_ts, tele_duration, tele_samples,
tele_first_pos, tele_last_pos.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.export_validation_csv import (
    CACHE, db_path_for, read_raw, read_db, fmt_ts, fmt_dur, norm_laptime,
    wc_to_ms, ms_to_wc, event_name, session_name, seg_at_offset,
)

# Usage: export_fp_csv.py [out_basename target_rel ...]
#   default → Barcelona FP1+FP2 → tmp/fp_laps.csv
_args = sys.argv[1:]
OUT = Path(__file__).resolve().parent.parent / "tmp" / (_args[0] if _args else "fp_laps.csv")
TARGETS = _args[1:] if len(_args) > 1 else \
    ["1287_Barcelona/11300_Practice_1", "1287_Barcelona/11301_Practice_2"]


def main():
    rows = []
    for rel in TARGETS:
        sdir = CACHE / rel
        db = db_path_for(sdir)
        if not db.exists():
            print(f"missing DB for {rel}")
            continue
        ev = event_name(sdir.parent.name)
        sess = session_name(sdir.name)
        is_quali = "Qualifying" in sdir.name
        nol_ts, in_lap, out_lap = read_raw(sdir)
        tla, laptime, tele, seg_tl, start_wc = read_db(db)
        start_ms = wc_to_ms(start_wc) if start_wc else 0

        cars = sorted(set(nol_ts) | set(laptime) | set(tele),
                      key=lambda c: int(c) if c.isdigit() else 999)
        for car in cars:
            laps = set(nol_ts.get(car, {})) | set(laptime.get(car, {})) | set(tele.get(car, {}))
            for lap in sorted(laps):
                ndt = nol_ts.get(car, {}).get(lap)
                qpart = ""
                if is_quali and ndt is not None and seg_tl:
                    qpart = seg_at_offset(seg_tl, wc_to_ms(fmt_ts(ndt)) - start_ms)
                ts_start = ts_end = tdur = ""
                nsamp = first_pos = last_pos = ""
                if lap in tele.get(car, {}):
                    end_wc, samples = tele[car][lap]
                    nsamp = len(samples)
                    if samples:
                        tvals = [s[6] for s in samples]
                        dur = max(tvals) - min(tvals)
                        tdur = fmt_dur(dur)
                        ts_end = end_wc
                        ts_start = ms_to_wc(wc_to_ms(end_wc) - dur)
                        first_pos = f"{samples[0][0]:.2f}"
                        last_pos = f"{samples[-1][0]:.2f}"
                rows.append([
                    ev, sess, qpart, car, tla.get(car, car), lap,
                    "y" if lap in out_lap.get(car, set()) else "n",
                    "y" if lap in in_lap.get(car, set()) else "n",
                    fmt_ts(ndt) if ndt else "",
                    norm_laptime(laptime.get(car, {}).get(lap, "")),
                    ts_start, ts_end, tdur, nsamp, first_pos, last_pos,
                ])

    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "session", "quali_part", "car", "tla", "lap",
                    "out_lap", "in_lap", "nol_ts", "lap_time",
                    "tele_start_ts", "tele_end_ts", "tele_duration", "tele_samples",
                    "tele_first_pos", "tele_last_pos"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
