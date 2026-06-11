# UI rewire — testing issues (do NOT fix until user finishes testing)

Branch `review/frontend-rewire`. User testing each session; many issues are common.
Root-cause notes from read-only investigation of the kept transient DBs (REPLAY_DEBUG=1).

## Melbourne FP1 (+ likely all sessions)

### I1 — Driver TLA/colour styling: row 1 vs row 2 differ
1st row TLA text + team-colour swatch smaller than 2nd row's (should match).
Likely the `p1` class on idx 0 (`buildRow`) styled differently in standings.css.
→ CLARIFIED: it's the START identifier (.driver-tla/.driver-color) vs the END
mirror (.driver-tla-end/.driver-color-end). Current source has NO `-end` CSS
rule anywhere, identical markup, identical grid tracks (14px colour / 5fr tla
both ends) → fresh assets cannot differ. LIKELY CAUSE: stale cached standings.css
(it was loaded with NO ?v= cache-buster, so cached indefinitely; an older version
had `-end` sizing). FIX applied: added ?v=2026-06-11a to all tile CSS + bumped
all JS/CSS cache-busters → hard-refresh and re-check. If it persists, send the
computed font-size/height of the two TLA spans.

### I2 — Replay scrubber shows a "LIVE" marker at the end
The `scrubberLive` element in renderEventMarkers is gated on `!hasSessionEnd`,
not on `messageBus.isLive`. In replay it still renders.
→ FIX: header.js renderEventMarkers — gate scrubberLive on `messageBus.isLive`.

### I3 — Lap 1 (out lap) rendered as a timed lap
Out/in laps still show a lap time. The message should carry an out/in flag so
the client doesn't render the time as a representative lap (still keep the time
available).
→ FIX (backend): driverLaps.laps records should include an out/in/pit flag per
lap (lap_timing_processor). Client then suppresses the timed-lap rendering for
flagged laps. (driverLapClassification has per-lap type but standings doesn't
cross-ref it for the lap-time cell.)

### I4 — Mini-sectors not width-invariant / jittery
driverMiniSectors only carries the *defined* segments (sticky deltas → partial),
so the array length varies → client layout oscillates (e.g. S1 segments fill full
width, then snap narrower as later sectors arrive).
→ FIX (backend): sector_timing_processor must always emit ALL mini-sectors
explicitly (fixed length, nulls for not-yet-set) so the layout is fixed. Client
layout should be width-invariant regardless of how many are coloured.
(Currently client derives SEGMENT_LAYOUT from miniSectors lengths — contributes
to the oscillation; real fix is fixed-length backend payload.)
→ FIXED (backend): sector_timing now tracks the max segment count seen per
sector (track-wide) and pads every driverMiniSectors emit to it → fixed-length
arrays, width-invariant render. Stable after the first complete lap.
→ REFINEMENT (later): user notes trackGeometry carries mini-sector info — but
its `sectors` field is the 20 marshal sectors with pct ranges, without the
S1/S2/S3 split, so using it for per-timing-sector counts needs marshal→timing
mapping. Would make the layout stable from lap 1 (vs after lap 1). Deferred.

### I5 — Telemetry lap misidentification (BACKEND data bug) — happens a LOT
CONFIRMED via FP1 DB. telemetryLap lap numbering is off by +1 from lap ~4 on,
plus a broken out→first-flying-lap transition:
- LAW(30): telem L4 dur 1:49.1 = driverLaps **L3**; L5=1:25.5=L4; L6=1:57.0=L5;
  L7=1:24.0=L6; L8=2:00.6=L7  → telem lap N = timing lap N−1.
- telem L1 = out lap (~2:01, ≈ driverLaps L1). telem **L2 MISSING** (the first
  flying lap, driverLaps L2 1:58.7) and a spurious 2-sample **L3** (7ms) appears.
- NOR(1): telem laps present 2,4,6,7 (missing 1,3,5); L2 = 10:28 flat-line aggregate.
So the client faithfully shows wrong backend lap numbers; "L2 not selectable" =
no telemetryLap:30:2 row.
→ FIX (backend): telemetry_processor must number each COMPLETED lap with the
authoritative just-ended lap number (align with driverLaps lastLap.lap / NoL
P-Q semantics, i.e. N−1 of the starting NoL), and not emit spurious degenerate
laps at the out→flying transition. Re-derive crossing→lap assignment.
→ FIXED (backend, telemetry_processor): completed-lap telemetry is now keyed
on the authoritative driverLaps.lastLap.lap with emit-or-defer matching to the
latest S/F crossing (absorbs a spurious extra crossing; survives the
timing/position arrival-order race). Verified on FP1: LAW + NOR flying laps now
align exactly with driverLaps (was +1 with a missing/degenerate lap at the
out→flying transition). Client needed NO change — all pills are already
clickable; "L2 not selectable" was just the missing/misnumbered telemetryLap row.
CAVEAT: garage-heavy laps with no clean S/F crossing (e.g. NOR L2=10:28 sitting
in the garage) inherently can't be bounded → those remain missing/merged. Expected.

### I6 — Chequered flag / session-finished not shown as a scrubber event
The CHEQUERED (and session end) marker isn't appearing on the playback scrubber.
→ INVESTIGATE: track_status emits `event` "CHEQUERED"; playback_event emits
`playbackEvent` sessionEnd. renderEventMarkers reads state.events (from state:full
events = DB topics 'event','playbackEvent'). Check the chequered `event` row is
present + that renderEventMarkers maps it (CHEQUERED branch exists). May be the
event isn't emitted/persisted, or filtered.

### I7 — Chequered flag icon in driver status (styling)
Add a 0.5px border around the flag; change dimensions 16x14 → 16x12; adjust the
square grid to fit.
→ FIX: standings.js CHEQUERED_SVG.

## Melbourne Q

### I8 — KO-zone gap measured against CutOffTime (107%), not the cutoff car
Q1 (16:10:38): P17 PER / P18 BOT show gaps −1.861 / −1.787 — that's
`bestMs − CutOffTime` (107% of P1), which is meaningless.
CONFIRMED in driver_gap_processor `_handle_quali`: in-zone gap =
`_fmt_gap(bms - self._cutoff_time_ms)`.
→ FIX (backend): a KO-zone driver's gap should be to the car in the last
ADVANCING position (P16 in Q1 / P10 in Q2 = `_cutoff_position()`), i.e.
`bms − best_ms[driver_at_cutoff_pos]` (positive = how far from safety). Drop
the CutOffTime-based gap. (Supersedes the old "CutOffTime still used for gap
value" note in memory — update that memory when fixing.)

## Cross-session (to confirm as more sessions tested)
- (append)
