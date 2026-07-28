#!/usr/bin/env python3
"""Measure telemetry_processor's CURRENT CarData/Position pairing yield.

Baseline-establishment script for work block WB-3
(docs/artifacts/2026-07-27-007-project-kickoff/test-battery-plan.md §5, WB-3),
answering G1 in backend-synthesis.md §2: "the `.z` pairing yield has a ~82%
ceiling even sorted, and nobody has asked why". This script measures that
number by running the REAL pipeline, not a simulation of the pairing rule.

Methodology
-----------
This runs `SessionPreProcessor.run()` — the actual production build path —
against each populated fixture in `regression/golden/*`, with the real
`file_reader` (today's payload-timestamp-sorted reorder buffer), the real
`position_processor` (real track-geometry projection, real Position.z
decompression), and the real, UNMODIFIED `telemetry_processor.TelemetryProcessor`.

Two counters are added by subscribing two ADDITIONAL handlers to the same
`SessionMessageBus` `telemetry_processor.py` itself uses — not by editing or
monkeypatching `telemetry_processor.py`, and not by re-simulating its pairing
rule:

  * denominator — "CarData candidates": every (entry, car) pair inside a
    `CarData.z` message that has a `Channels` dict and a car number <= 99.
    This mirrors, VERBATIM, the same validity filter
    `TelemetryProcessor._handle_car_data` applies (see
    app/processing/processors/telemetry_processor.py:437-447) before it even
    looks at `pending_pos`. It is counted by a second subscriber on the same
    "CarData.z" topic (the message bus already supports more than one
    subscriber per topic — position_processor and telemetry_processor both
    already subscribe to it), so it sees exactly the same messages
    telemetry_processor sees, in the same real pipeline run.

  * numerator — "paired": every `liveTelemetry:{num}` message emitted, via a
    wildcard subscriber. telemetry_processor emits exactly one of these per
    successful pairing (app/processing/processors/telemetry_processor.py:488)
    and none on a skip (line 449-450: "no pending position to pair with ->
    skip this CarData"). This is the REAL pairing decision, not a
    reimplementation of it — the wildcard subscriber only counts what the
    unmodified processor already decided to emit.

  yield = paired / cardata_candidates

This is a full-pipeline measurement: SignalR envelope parsing, the .z
decompress/split, the payload-timestamp reorder buffer, the session gate, the
1-hour cutoff filter, and the real `position_processor` (with real track
geometry for the fixture's circuit) all run exactly as they do in production.
The only thing added is two read-only counters; nothing in the pipeline's
decision-making is altered, and no application file is modified by this
script.

Caveats, stated plainly
------------------------
* `position_processor` needs a live track SVG (`static/images/tracks/*.svg`,
  bundled in the repo, not user data) and, optionally, a circuit-signature
  file for the DTW dp-recovery path; a missing signature just leaves the
  simpler geometry-only projection active (see position_processor.py
  docstring) — it does not block the "position" topic from being emitted, so
  it does not gate this measurement's validity, only its use of the DTW path.
* `pending_pos` is set the moment a valid Position arrives, INCLUDING before
  session activation (`_handle_position`, "Pre-race... run NO S/F/lap logic
  and store no samples") and while a car is in the pit. So the denominator
  and numerator both include pre-race and in-pit pairings, matching what the
  real code actually pairs and emits — this script does not filter those out,
  because filtering them would no longer be measuring what the shipped code
  does.
* This DELIBERATELY measures against TODAY's working, sorted-order pipeline
  (the reorder buffer + payload-timestamp sort in `file_reader.py`, unchanged
  by this script) — the ~82% figure D7-B is meant to be checked against, per
  backend-synthesis.md G1. It does not measure file-order (unsorted) yield;
  that number already exists (architecture-plan.md §A.7.1) and is not
  D7-B's baseline.
* Uses a scratch `F1_DATA_HOME` (a temp directory, deleted after the run) so
  no real user data-home is touched, and each golden fixture directory is
  read-only for the whole run — only `live.jsonl` inside it is read; the
  transient SQLite DB this build produces is written under the scratch
  data-home's `tmp/`, never inside the fixture directory.

Usage
-----
    python3 utils/scripts/measure_telemetry_pairing_baseline.py \
        [--cases spa-q-live,spa-race-live] [--out docs/telemetry_pairing_baseline.json]

Run from `repos/dev` (needs the `app` package importable and the real
dependencies — numpy etc — the venv used for the app itself).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent   # repos/dev
GOLDEN = REPO_ROOT.parent.parent / "regression" / "golden"  # ../../regression/golden

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _discover_cases(golden: Path) -> dict[str, Path]:
    """case name -> populated session dir (mirrors regression/regress.py's
    discover_cases, so this script and the regression harness always agree on
    what counts as 'populated')."""
    out: dict[str, Path] = {}
    if not golden.exists():
        return out
    for case in sorted(p for p in golden.iterdir() if p.is_dir()):
        sessions = [d for d in case.iterdir() if d.is_dir() and (d / "live.jsonl").exists()]
        if len(sessions) == 1:
            out[case.name] = sessions[0]
        elif sessions:
            raise SystemExit(f"ERROR: {case} holds {len(sessions)} session dirs; expected exactly 1")
    return out


def _is_valid_cardata_candidate(entry: Any) -> list[str]:
    """Verbatim mirror of the validity filter
    TelemetryProcessor._handle_car_data applies before checking pending_pos
    (telemetry_processor.py:437-447). Returns the list of car numbers in
    `entry` that would reach the pending_pos check."""
    if not isinstance(entry, dict):
        return []
    cars = entry.get("Cars")
    if not isinstance(cars, dict):
        return []
    out = []
    for num, car in cars.items():
        try:
            if int(num) > 99:
                continue
        except (TypeError, ValueError):
            continue
        if not isinstance(car, dict):
            continue
        ch = car.get("Channels")
        if not isinstance(ch, dict):
            continue
        out.append(num)
    return out


async def measure_one(case: str, session_path: Path, data_home: Path) -> dict:
    """Run the REAL pipeline (SessionPreProcessor.run()) for one fixture and
    return the pairing-yield counters. Imports app.* lazily, after
    F1_DATA_HOME is set in the environment (module-level DATA_HOME is
    computed at import time)."""
    from app.processing.preprocessor import SessionPreProcessor

    counters = {"cardata_candidates": 0, "paired": 0}

    class InstrumentedPreProcessor(SessionPreProcessor):
        """Adds two READ-ONLY counting subscribers to the same message bus
        telemetry_processor.py itself subscribes to. Does not alter, wrap, or
        monkeypatch telemetry_processor.py or any other processor; the real
        _init_processors() runs first, unmodified, then this subscribes its
        own additional handlers to the same bus, the same way any other
        processor already does (the bus supports multiple subscribers per
        topic by design — see message_bus.py)."""

        def _init_processors(self) -> None:
            super()._init_processors()

            def _count_cardata_candidates(data: Any, clock_time) -> None:
                if not isinstance(data, dict):
                    return
                entries = data.get("Entries")
                if not isinstance(entries, list):
                    return
                for entry in entries:
                    counters["cardata_candidates"] += len(_is_valid_cardata_candidate(entry))

            def _count_paired(topic: str, data: Any, clock_time) -> None:
                if topic.startswith("liveTelemetry:"):
                    counters["paired"] += 1

            self._bus.on("CarData.z", _count_cardata_candidates)
            self._bus.on("*", _count_paired)

    pp = InstrumentedPreProcessor(session_path, "")
    t0 = time.monotonic()
    await pp.run(force=True)
    elapsed = time.monotonic() - t0
    pp.close()

    yield_pct = (100.0 * counters["paired"] / counters["cardata_candidates"]
                 if counters["cardata_candidates"] else None)
    return {
        "case": case,
        "session": session_path.name,
        "cardata_candidates": counters["cardata_candidates"],
        "paired": counters["paired"],
        "yield_pct": round(yield_pct, 4) if yield_pct is not None else None,
        "build_failed": pp.failed,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", help="comma-separated subset of golden/ case names (default: all populated)")
    ap.add_argument("--golden", default=str(GOLDEN), help="path to regression/golden")
    ap.add_argument("--out", default=str(REPO_ROOT / "docs" / "telemetry_pairing_baseline.json"),
                     help="where to write the baseline JSON")
    a = ap.parse_args()

    golden = Path(a.golden)
    cases = _discover_cases(golden)
    if not cases:
        sys.exit(f"ERROR: no populated golden fixtures found at {golden}")
    if a.cases:
        want = [c.strip() for c in a.cases.split(",")]
        missing = [c for c in want if c not in cases]
        if missing:
            sys.exit(f"ERROR: unknown/empty case(s): {', '.join(missing)}\n"
                     f"available: {', '.join(cases)}")
        cases = {k: cases[k] for k in want}

    scratch = Path(tempfile.mkdtemp(prefix="f1u_pairing_baseline_"))
    os.environ["F1_DATA_HOME"] = str(scratch)
    (scratch / "tmp").mkdir(parents=True, exist_ok=True)

    print(f"scratch data home: {scratch}")
    print(f"cases: {', '.join(cases)}\n")

    results = []
    try:
        for case, session in cases.items():
            print(f"  measuring {case:<16} ({session.name}) ...", end=" ", flush=True)
            r = asyncio.run(measure_one(case, session, scratch))
            results.append(r)
            if r["build_failed"]:
                print(f"BUILD FAILED ({r['elapsed_s']}s)")
            else:
                print(f"{r['paired']:>6,} / {r['cardata_candidates']:>6,} "
                      f"= {r['yield_pct']}%  ({r['elapsed_s']}s)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    ok = [r for r in results if not r["build_failed"] and r["cardata_candidates"]]
    overall_paired = sum(r["paired"] for r in ok)
    overall_candidates = sum(r["cardata_candidates"] for r in ok)
    overall_pct = (round(100.0 * overall_paired / overall_candidates, 4)
                   if overall_candidates else None)

    baseline = {
        "_description": ("Real full-pipeline CarData/Position pairing yield, measured "
                          "against today's WORKING sorted-order file_reader.py + "
                          "position_processor.py + UNMODIFIED telemetry_processor.py "
                          "(D7-B has not yet landed). yield = liveTelemetry:* emissions "
                          "/ CarData entries with a valid per-driver Channels dict "
                          "(same filter telemetry_processor._handle_car_data applies). "
                          "Established by work block WB-3 "
                          "(test-battery-plan.md, backend-synthesis.md G1)."),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": {
            "paired": overall_paired,
            "cardata_candidates": overall_candidates,
            "yield_pct": overall_pct,
        },
        "per_fixture": {r["case"]: r for r in results},
    }

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(baseline, indent=2, sort_keys=False) + "\n")
    print(f"\noverall yield: {overall_pct}%  ({overall_paired:,} / {overall_candidates:,})")
    print(f"baseline written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
