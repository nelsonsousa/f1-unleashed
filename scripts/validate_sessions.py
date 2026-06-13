"""Data-validation pass over every cached/processed 2026 session.

Reports discrepancies ONLY — no cause analysis, no fixes.

Sources:
  * raw live.jsonl  -> NoL (NumberOfLaps) + LastLapTime ground truth, with the
    envelope DateTime as the message timestamp;
  * processed DB    -> driverLaps (thin, accumulate lastLap), driverStatus,
    driverLapClassification, telemetryLap:{d}:{lap}.

Checks implemented here (the ones that need no external/official data):
  2.1  NoL lap count  vs  count of lap times received (±1, last lap excepted)
  4.1  every lap has a classification
  4.2  IN/OUT classifications align with PIT/OUT driverStatus
  4.3  PUSH/SLOW classifications consistent with lap times
  5.1  telemetry-lap count  vs  NoL lap count
  5.2  (excl IN/OUT) telemetry elapsed ~= lap time (<=1s)
  5.3  (excl outages) uniform ~3-4 Hz sample coverage
  5.4  (excl outages) no near-empty telemetry laps
  5.5  (excl IN/OUT) telemetry lap start/end offsets ~= NoL boundary offsets (<=1s)
  5.6  telemetry lap dp is monotonic (flag backward steps)

Official-classification checks (2.2, 3.1) are handled separately.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data/livetiming_cache/2026"
TMP = ROOT / "tmp"

# Sample-rate expectations (3-4 Hz => 250-333 ms spacing).
HZ_DT_MIN = 0.20
HZ_DT_MAX = 0.45
OUTAGE_DT = 1.0          # gap > 1s between samples = outage, excluded from rate stat
EMPTY_MIN_SAMPLES = 8    # fewer "real" samples than this = near-empty (excl outage)
EMPTY_MIN_DP_RANGE = 50  # dp span (max-min) below this (and not in/out) = near-empty
TOL_S = 1.0              # 1s tolerance for time/offset matches

IN_OUT_TYPES = {"OUT", "IN", "PIT"}


def _parse_ms(s):
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


def db_path_for(session_dir: Path) -> Path:
    safe = f"2026_{session_dir.parent.name}_{session_dir.name}"
    return TMP / f"{safe}.db"


# ── raw live.jsonl: NoL + LastLapTime ground truth ──────────────────────────
def read_raw(session_dir: Path):
    """Return per-driver:
        nol_seq:   [(dt, nol)] in arrival order (NumberOfLaps present)
        llt_seq:   [(dt, value)] LastLapTime with a Value (in arrival order)
    """
    nol = defaultdict(list)
    llt = defaultdict(list)
    f = session_dir / "live.jsonl"
    for line in f.open():
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
        j = m.get("Json")
        lines = j.get("Lines") if isinstance(j, dict) else None
        if not isinstance(lines, dict):
            continue
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "NumberOfLaps" in d:
                nol[num].append((dt, d["NumberOfLaps"]))
            llv = d.get("LastLapTime")
            if isinstance(llv, dict) and llv.get("Value"):
                llt[num].append((dt, llv["Value"]))
    return nol, llt


# ── processed DB ────────────────────────────────────────────────────────────
def read_db(db: Path):
    con = sqlite3.connect(db)
    out = {
        "driverLaps": defaultdict(list),     # num -> [(off, data)]
        "driverStatus": defaultdict(list),   # num -> [(off, status)]
        "lapCls": defaultdict(list),         # num -> [(off, data)]
        "telemetry": defaultdict(dict),      # num -> {lap: (off, samples)}
        "session_type": None,
    }
    meta = dict(con.execute("SELECT key,value FROM processing_meta").fetchall())
    out["session_type"] = meta.get("session_type")
    for off, topic, data in con.execute(
            "SELECT offset_ms,topic,data FROM messages ORDER BY offset_ms"):
        if topic.startswith("driverLaps:"):
            out["driverLaps"][topic.split(":", 1)[1]].append((off, json.loads(data)))
        elif topic.startswith("driverStatus:"):
            out["driverStatus"][topic.split(":", 1)[1]].append((off, json.loads(data)))
        elif topic.startswith("driverLapClassification:"):
            out["lapCls"][topic.split(":", 1)[1]].append((off, json.loads(data)))
        elif topic.startswith("telemetryLap:"):
            _, num, lap = topic.split(":")
            out["telemetry"][num][int(lap)] = (off, json.loads(data))
    con.close()
    return out


def laptimes_from_driverlaps(rows):
    """Accumulate {lap: time_str} from thin driverLaps lastLap; also the
    currentLap timeline [(off, currentLap)] and max currentLap."""
    times = {}
    cur_timeline = []
    last_cur = None
    for off, d in rows:
        cur = d.get("currentLap")
        if cur is not None and cur != last_cur:
            cur_timeline.append((off, cur))
            last_cur = cur
        ll = d.get("lastLap")
        if isinstance(ll, dict) and ll.get("lap") is not None and ll.get("time"):
            times[int(ll["lap"])] = ll["time"]
    return times, cur_timeline


def lap_boundaries(cur_timeline):
    """offset at which each lap N became current -> {lap: start_off}."""
    starts = {}
    for off, cur in cur_timeline:
        if cur not in starts:
            starts[cur] = off
    return starts


def final_cls_per_lap(rows):
    """{lap: final_type} (last non-degenerate type wins) + all (off,lap,type)."""
    by_lap = {}
    for off, d in rows:
        lap = d.get("lap")
        typ = d.get("type")
        if lap is None:
            continue
        # keep the latest type for the lap; prefer a non-empty type
        prev = by_lap.get(lap)
        if prev is None or typ:
            by_lap[lap] = typ if typ else (prev or "")
    return by_lap


def status_lap_at(status_rows, cur_timeline):
    """Map each PIT/OUT status event to the lap current at its offset."""
    def lap_at(off):
        lap = None
        for o, c in cur_timeline:
            if o <= off:
                lap = c
            else:
                break
        return lap
    pit_laps, out_laps = set(), set()
    for off, st in status_rows:
        L = lap_at(off)
        if L is None:
            continue
        if st == "PIT":
            pit_laps.add(L)
        elif st == "OUT":
            out_laps.add(L)
    return pit_laps, out_laps


def validate(session_dir: Path, db: Path, disc: list):
    name = f"{session_dir.parent.name}/{session_dir.name}"
    if not db.exists():
        disc.append((name, "DB", "missing DB — not built"))
        return
    is_race = any(k in session_dir.name for k in ("Race", "Sprint")) \
        and "Sprint_Qualifying" not in session_dir.name
    nol_raw, llt_raw = read_raw(session_dir)
    d = read_db(db)
    drivers = sorted(set(nol_raw) | set(d["driverLaps"]), key=lambda x: int(x) if x.isdigit() else 999)

    for num in drivers:
        tag = f"car {num}"
        nol_seq = nol_raw.get(num, [])
        nol_vals = [v for _, v in nol_seq]
        max_nol = max(nol_vals) if nol_vals else None
        # Completed laps the NoL counter credits: race NoL=N => lap N ended;
        # P/Q NoL=N => lap N starting, so N-1 completed.
        nol_laps = (max_nol if is_race else max_nol - 1) if max_nol is not None else 0
        n_llt = len(llt_raw.get(num, []))

        dl_rows = d["driverLaps"].get(num, [])
        times, cur_tl = laptimes_from_driverlaps(dl_rows)
        starts = lap_boundaries(cur_tl)
        n_times = len(times)
        cls_rows = d["lapCls"].get(num, [])
        cls = final_cls_per_lap(cls_rows)
        tele = d["telemetry"].get(num, {})

        # 2.1 NoL count vs lap-times-received
        if max_nol is not None and abs(nol_laps - n_llt) > 1:
            disc.append((name, tag, f"[2.1] NoL laps={nol_laps} but lap-times received={n_llt} (diff {nol_laps-n_llt})"))

        # 4.1 every lap classified  (laps 1..max_completed should each have a type)
        max_completed = max(times) if times else (max_nol - 1 if max_nol else 0)
        missing_cls = [L for L in range(1, (max_completed or 0) + 1)
                       if L not in cls or not cls.get(L)]
        if missing_cls:
            disc.append((name, tag, f"[4.1] laps with no/empty classification: {missing_cls}"))

        # 4.2 IN/OUT classification vs PIT/OUT status
        pit_laps, out_laps = status_lap_at(d["driverStatus"].get(num, []), cur_tl)
        cls_out = {L for L, t in cls.items() if t == "OUT"}
        cls_pit = {L for L, t in cls.items() if t in ("PIT", "IN")}
        out_no_status = sorted(cls_out - out_laps)
        pit_no_status = sorted(cls_pit - pit_laps)
        if out_no_status:
            disc.append((name, tag, f"[4.2] laps classified OUT with no OUT driverStatus on that lap: {out_no_status}"))
        if pit_no_status:
            disc.append((name, tag, f"[4.2] laps classified PIT/IN with no PIT driverStatus on that lap: {pit_no_status}"))

        # 4.3 PUSH/SLOW consistency: SLOW laps should be slower than the driver's
        # best PUSH/representative lap; PUSH laps should be near the best.
        push = {L: _parse_ms(times[L]) for L, t in cls.items() if t == "PUSH" and L in times and _parse_ms(times[L])}
        slow = {L: _parse_ms(times[L]) for L, t in cls.items() if t == "SLOW" and L in times and _parse_ms(times[L])}
        if push:
            best_push = min(push.values())
            for L, ms in slow.items():
                if ms < best_push:
                    disc.append((name, tag, f"[4.3] lap {L} classified SLOW ({times[L]}) faster than best PUSH ({best_push/1000:.3f}s)"))
            for L, ms in push.items():
                if ms > best_push * 1.07:
                    disc.append((name, tag, f"[4.3] lap {L} classified PUSH ({times[L]}) >107% of best PUSH"))

        # 5.1 telemetry-lap count vs NoL lap count
        n_tele = len(tele)
        if max_nol is not None and abs(n_tele - nol_laps) > 1:
            disc.append((name, tag, f"[5.1] telemetry laps={n_tele} but NoL laps={nol_laps} (diff {n_tele-nol_laps})"))

        # per-telemetry-lap checks
        for lap in sorted(tele):
            off, samples = tele[lap]
            if not samples:
                disc.append((name, tag, f"[5.4] telemetry lap {lap} is empty (0 samples)"))
                continue
            dps = [s[0] for s in samples]
            ts = [s[6] for s in samples]   # t_ms_rel
            is_in_out = cls.get(lap) in IN_OUT_TYPES
            dp_range = max(dps) - min(dps)
            n_real = len(samples)

            # 5.6 monotonic dp (allow tiny epsilon)
            backsteps = sum(1 for i in range(1, len(dps)) if dps[i] < dps[i-1] - 0.5)
            if backsteps:
                disc.append((name, tag, f"[5.6] telemetry lap {lap} non-monotonic dp ({backsteps} backward steps)"))

            # sample spacing (drop outage gaps)
            gaps = [(ts[i]-ts[i-1])/1000.0 for i in range(1, len(ts))]
            non_outage = [g for g in gaps if 0 < g <= OUTAGE_DT]
            outages = [g for g in gaps if g > OUTAGE_DT]
            med_dt = sorted(non_outage)[len(non_outage)//2] if non_outage else None

            # 5.4 near-empty (exclude in/out; outage-aware via dp_range)
            if not is_in_out and (n_real < EMPTY_MIN_SAMPLES or dp_range < EMPTY_MIN_DP_RANGE):
                disc.append((name, tag, f"[5.4] telemetry lap {lap} near-empty: {n_real} samples, dp range {dp_range:.0f}%"))

            # 5.3 sample rate (exclude in/out and outage gaps)
            if not is_in_out and med_dt is not None and not (HZ_DT_MIN <= med_dt <= HZ_DT_MAX):
                disc.append((name, tag, f"[5.3] telemetry lap {lap} median sample spacing {med_dt*1000:.0f}ms (not ~3-4Hz)"))

            # 5.2 elapsed vs lap time (exclude in/out)
            if not is_in_out and lap in times:
                lt = _parse_ms(times[lap])
                elapsed = ts[-1] - ts[0]
                if lt and abs(elapsed - lt) > TOL_S*1000:
                    disc.append((name, tag, f"[5.2] telemetry lap {lap} elapsed {elapsed/1000:.2f}s vs lap time {lt/1000:.2f}s (diff {(elapsed-lt)/1000:.2f}s)"))

            # 5.5 start/end offsets vs NoL boundaries (exclude in/out)
            if not is_in_out and lap in starts and (lap+1) in starts:
                tele_end_off = off
                tele_start_off = off - (ts[-1] - ts[0])
                nol_start = starts[lap]
                nol_end = starts[lap+1]
                if abs(tele_start_off - nol_start) > TOL_S*1000:
                    disc.append((name, tag, f"[5.5] telemetry lap {lap} start off {tele_start_off:.0f}ms vs NoL boundary {nol_start}ms (diff {(tele_start_off-nol_start)/1000:.2f}s)"))
                if abs(tele_end_off - nol_end) > TOL_S*1000:
                    disc.append((name, tag, f"[5.5] telemetry lap {lap} end off {tele_end_off:.0f}ms vs NoL boundary {nol_end}ms (diff {(tele_end_off-nol_end)/1000:.2f}s)"))


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    disc = []
    sess = []
    for event in sorted(CACHE.iterdir()):
        if not event.is_dir():
            continue
        for s in sorted(event.iterdir()):
            if (s / "live.jsonl").exists():
                if only and only not in f"{event.name}/{s.name}":
                    continue
                sess.append(s)
    for s in sess:
        validate(s, db_path_for(s), disc)

    # group by session
    by_sess = defaultdict(list)
    for name, tag, msg in disc:
        by_sess[name].append((tag, msg))
    for name in sorted(by_sess):
        print(f"\n## {name}  ({len(by_sess[name])} discrepancies)")
        for tag, msg in by_sess[name]:
            print(f"  - {tag}: {msg}")
    print(f"\nTOTAL discrepancies: {len(disc)} across {len(by_sess)} sessions")


if __name__ == "__main__":
    main()
