"""End-of-session lap reclassification.

Refines the live LapClassificationProcessor's output once all lap times
for a session are known. The live classifier has to label every lap the
moment it ends; with the full session in hand we can re-evaluate against
the whole pattern of times.

Rules — for each driver:

  • OUT / IN / PIT are untouched (they come from physical position).
  • LONG run: ≥ 2 consecutive timed laps in the same stint where every
    adjacent pair is within ``LONG_TOL_MS`` AND every member is within
    ``LONG_PACE_CUTOFF_MS`` of the driver's session-best timed lap.
    All members → LONG.
  • For each remaining timed lap:
      - ``lap_time > best + PUSH_CUTOFF_MS`` → COOL
      - else if it's a **local minimum** in its stint's timed-lap
        sequence (strictly faster than both neighbours, or its only
        one) → PUSH
      - else → COOL

Used both by ``scripts/reclassify_laps.py`` (standalone CSV audit) and
by the preprocessor's finalize step (writes corrected classifications
back to the session DB).
"""

from typing import Iterable

# Thresholds (milliseconds).
PUSH_CUTOFF_MS = 8000        # PUSH lap must be ≤ best + this
LONG_TOL_MS = 3500           # consecutive timed laps within this gap → LONG run
LONG_PACE_CUTOFF_MS = 10000  # members of a LONG run must be ≤ best + this
MIN_LONG_RUN = 2

UNTIMED = {"OUT", "IN", "PIT"}


def split_into_stints(laps: list[dict]) -> list[list[dict]]:
    """A stint runs from after a PIT/IN until the next PIT/IN. Each
    dict needs keys ``lap``, ``current_class``, ``lap_time_ms``."""
    stints: list[list[dict]] = []
    cur: list[dict] = []
    for l in laps:
        cur.append(l)
        if l["current_class"] in ("IN", "PIT"):
            stints.append(cur)
            cur = []
    if cur:
        stints.append(cur)
    return stints


def _find_long_runs(stint: list[dict], long_pace_max_ms: int) -> set[int]:
    """Lap numbers in `stint` belonging to a LONG run."""
    timed = [(i, l) for i, l in enumerate(stint)
             if l["current_class"] not in UNTIMED and l["lap_time_ms"] is not None]
    if len(timed) < MIN_LONG_RUN:
        return set()

    long_lap_nums: set[int] = set()

    def close_run(start: int, end: int) -> None:
        if end - start < MIN_LONG_RUN:
            return
        if not all(timed[j][1]["lap_time_ms"] <= long_pace_max_ms for j in range(start, end)):
            return
        for j in range(start, end):
            long_lap_nums.add(timed[j][1]["lap"])

    run_start = 0
    for i in range(1, len(timed)):
        lap_prev = timed[i - 1][1]
        lap_cur = timed[i][1]
        consec = (lap_cur["lap"] == lap_prev["lap"] + 1)
        within = abs(lap_cur["lap_time_ms"] - lap_prev["lap_time_ms"]) <= LONG_TOL_MS
        if not (consec and within):
            close_run(run_start, i)
            run_start = i
    close_run(run_start, len(timed))
    return long_lap_nums


def reclassify_driver(laps: list[dict]) -> None:
    """Mutates each lap's ``new_class`` field in place."""
    timed_times = [l["lap_time_ms"] for l in laps
                   if l["current_class"] not in UNTIMED and l["lap_time_ms"] is not None]
    if not timed_times:
        return
    best_ms = min(timed_times)
    push_max_ms = best_ms + PUSH_CUTOFF_MS
    long_pace_max_ms = best_ms + LONG_PACE_CUTOFF_MS

    for stint in split_into_stints(laps):
        long_laps = _find_long_runs(stint, long_pace_max_ms)
        timed = [l for l in stint
                 if l["current_class"] not in UNTIMED and l["lap_time_ms"] is not None]
        for pos, l in enumerate(timed):
            if l["lap"] in long_laps:
                l["new_class"] = "LONG"
                continue
            if l["lap_time_ms"] > push_max_ms:
                l["new_class"] = "COOL"
                continue
            prev = timed[pos - 1] if pos > 0 else None
            nxt = timed[pos + 1] if pos + 1 < len(timed) else None
            is_local_min = True
            if prev is not None and prev["lap_time_ms"] < l["lap_time_ms"]:
                is_local_min = False
            if nxt is not None and nxt["lap_time_ms"] < l["lap_time_ms"]:
                is_local_min = False
            l["new_class"] = "PUSH" if is_local_min else "COOL"


def reclassify_session(
    per_driver_laps: dict[str, list[dict]],
) -> dict[str, dict[int, str]]:
    """Run the reclassification across every driver and return
    ``{driver_num: {lap_number: new_class}}``.

    `per_driver_laps` must map driver number → chronological list of
    dicts with keys ``lap``, ``current_class``, ``lap_time_ms``. The
    dicts are mutated in place (``new_class`` is set on each)."""
    out: dict[str, dict[int, str]] = {}
    for num, laps in per_driver_laps.items():
        # Initialise new_class to current_class so OUT/IN/PIT pass through.
        for l in laps:
            l.setdefault("new_class", l["current_class"])
        reclassify_driver(laps)
        out[num] = {l["lap"]: l["new_class"] for l in laps}
    return out
