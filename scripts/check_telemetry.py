"""Focused telemetry data-quality checks across all processed sessions.

1. telemetry-lap count == NoL-counted lap count (per driver)
2. empty telemetry laps (degenerate: too few samples / negligible dp span)
3. position wraps WITHIN a telemetry lap (dp should be monotonic 0->100)
4. telemetry laps that are neither IN(PIT) nor OUT must start at 0% and end 100%

IN/OUT laps are identified from driverStatus PIT/OUT mapped to the lap in
progress (independent of the classifier).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.validate_sessions import (
    CACHE, db_path_for, read_raw, read_db,
    laptimes_from_driverlaps, status_lap_at,
)

EMPTY_MIN_SAMPLES = 6      # fewer than this = empty/degenerate
EMPTY_MIN_DP_SPAN = 5.0    # dp span (max-min) below this = empty/degenerate
WRAP_BACKSTEP = 2.0        # dp drop > this between consecutive samples = internal wrap
START_EPS = 2.0            # clean lap first dp must be <= this
END_EPS = 98.0             # clean lap last dp must be >= this
CAP = 12                   # max detail lines per (session,check) before summarising


def is_race(name: str) -> bool:
    return any(k in name for k in ("Race", "Sprint")) and "Sprint_Qualifying" not in name


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    sessions = []
    for event in sorted(CACHE.iterdir()):
        if not event.is_dir():
            continue
        for s in sorted(event.iterdir()):
            if (s / "live.jsonl").exists() and (not only or only in f"{event.name}/{s.name}"):
                sessions.append(s)

    grand = defaultdict(int)
    for sdir in sessions:
        db = db_path_for(sdir)
        if not db.exists():
            continue
        name = f"{sdir.parent.name}/{sdir.name}"
        race = is_race(sdir.name)
        nol_raw, _ = read_raw(sdir)
        d = read_db(db)
        tele_all = d["telemetry"]

        c1, c2, c3, c4 = [], [], [], []
        drivers = sorted(set(nol_raw) | set(tele_all),
                         key=lambda x: int(x) if x.isdigit() else 999)
        for num in drivers:
            nol_vals = [v for _, v in nol_raw.get(num, [])]
            max_nol = max(nol_vals) if nol_vals else None
            completed = (max_nol if race else max_nol - 1) if max_nol else 0
            tele = tele_all.get(num, {})
            cur_tl = laptimes_from_driverlaps(d["driverLaps"].get(num, []))[1]
            pit_laps, out_laps = status_lap_at(d["driverStatus"].get(num, []), cur_tl)

            # 1: count
            tlaps = sorted(tele)
            missing = [L for L in range(1, (completed or 0) + 1) if L not in tele]
            extra = [L for L in tlaps if L > (completed or 0) or L < 1]
            if len(tlaps) != completed:
                c1.append(f"car {num}: telemetry={len(tlaps)} vs NoL={completed}"
                          + (f"  missing={missing}" if missing else "")
                          + (f"  extra={extra}" if extra else ""))

            for lap in tlaps:
                _, samples = tele[lap]
                dps = [s[0] for s in samples]
                if not dps:
                    c2.append(f"car {num} lap {lap}: 0 samples")
                    continue
                span = max(dps) - min(dps)
                # 2: empty
                if len(samples) < EMPTY_MIN_SAMPLES or span < EMPTY_MIN_DP_SPAN:
                    c2.append(f"car {num} lap {lap}: {len(samples)} samples, dp span {span:.0f}%")
                # 3: internal wraps
                backs = sum(1 for i in range(1, len(dps)) if dps[i] < dps[i-1] - WRAP_BACKSTEP)
                if backs:
                    c3.append(f"car {num} lap {lap}: {backs} backward dp step(s)")
                # 4: clean laps start 0 / end 100
                if lap not in pit_laps and lap not in out_laps:
                    if dps[0] > START_EPS or dps[-1] < END_EPS:
                        c4.append(f"car {num} lap {lap}: starts {dps[0]:.1f}% ends {dps[-1]:.1f}%")

        if not (c1 or c2 or c3 or c4):
            print(f"\n## {name}  — clean")
            continue
        print(f"\n## {name}")
        for label, lst in (("1 count mismatch", c1), ("2 empty laps", c2),
                           ("3 internal wraps", c3), ("4 not 0->100", c4)):
            grand[label] += len(lst)
            if not lst:
                continue
            print(f"  [{label}]  ({len(lst)})")
            for line in lst[:CAP]:
                print(f"    - {line}")
            if len(lst) > CAP:
                print(f"    … +{len(lst)-CAP} more")

    print("\n=== TOTALS ===")
    for k in ("1 count mismatch", "2 empty laps", "3 internal wraps", "4 not 0->100"):
        print(f"  {k}: {grand[k]}")


if __name__ == "__main__":
    main()
