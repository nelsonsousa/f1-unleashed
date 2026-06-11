# Overnight rewire — decisions log

Branch: `review/frontend-rewire` (off `review/redesign-laptiming-driverstatus`).
Working autonomously 2026-06-11 night; user to review in the morning.

## Locked choices (asked before sleep)
- **Branch**: new branch off current.
- **Backend scope**: full remaining backend incl. signalr reconnection review +
  timing_processor collapse, before the frontend. (Analysis / pace_prediction
  stays deferred per earlier call.)
- **Frontend scope**: full item #2 — rewire ALL tile consumers to the new
  per-driver/processed topics, PLUS the WS command protocol, Live button, and
  1× speed-lock for live sessions. Live-only paths can't be browser-tested until
  a live session.

## Plan / progress
### Phase 1 — backend cleanup
- [ ] Delete dead processors: playback_event, event_detector, session_data, run_plan
- [ ] Delete dead lap_reclassification.py
- [ ] Collapse timing_processor
- [ ] signalr_client reconnection review

### Phase 2 — frontend (full item #2)
- [ ] Map old→new topics + current tile consumers
- [ ] base.js: message bus / WS command protocol / restore / Live button / speed-lock
- [ ] Tile: standings
- [ ] Tile: track_map
- [ ] Tile: telemetry
- [ ] Tile: weather_radar
- [ ] Tile: race_control
- [ ] header (clocks, track status, playback/audio controls)

## Additional requests received overnight (queued, in order)
1. [DONE] signalr reconnection — backoff reconnect + re-subscribe; terminal
   `_SessionEnd` only on real stop; live_capture uses `is_alive` not
   `is_connected`. **UNTESTED against a live server** — validate at next live
   session. Verified via a stubbed reconnect-loop simulation.
2. [TODO] signalr dynamic topic discovery + alert for topics we don't process
   (ntfy on new ones; seed on first run). Implementing at the bus/preprocessor
   level (sees every raw topic, knows handlers, works live + replay).
3. [TODO] move cache out of the app folder to a standard OS data dir
   (~/.local/share or platform equivalent; Windows %LOCALAPPDATA%).
4. [TODO] Windows startup script (service.sh is mac/linux only).
5. [TODO — the main task] frontend rewire (full item #2).

## Decisions made (running)

### D1 — backend cleanup ordering (deviates slightly from "backend fully before frontend")
Dependency maps show two of the "delete" targets are load-bearing for the CURRENT
frontend / preprocessor, so deleting them before the UI rewire would break the build:
- **timing_processor**: still the sole emitter of `driverTiming/driverLastLap/
  driverLapTimes/driverTyres/segmentLayout` that the OLD tiles consume. The
  redesigned replacements (`driverLaps`, `driverSectors`, `driverMiniSectors`,
  `currentTyre`, `tyreHistory`) already exist. → delete timing_processor at the END
  of the frontend rewire, once tiles consume the new topics.
- **playback_event_processor**: more than display — the preprocessor uses it to
  anchor session start/end UTC (drives `offset_ms`) and audio start, and for the
  scrubber-event filter. → treat as a REFACTOR (relocate anchoring), not a blind
  delete; flagged for user. Deferring until after the header rewire.

So Phase 1 does the genuinely-safe backend work; the two coupled removals happen
within/after Phase 2. event_detector + run_plan are dead (unregistered, no
consumers) → deleted now. session_data → fold qualifyingPart into session_info.

### Old→new topic map (drives the frontend rewire)
- `display:standings` (one fat topic) → `standings` {drivers:[{num,position}]} (order
  only) + client JOINS per-driver topics: driverList, driverGap:{num}, driverLaps:{num}
  (bestLap/lastLap/laps), driverSectors:{num}, driverMiniSectors:{num}, driverStatus:{num},
  driverPenalties:{num}, currentTyre/tyreHistory:{num}, lapPrediction:{num},
  driverLapClassification:{num}, qualifyingSegment, raceLaps.
- `driverTiming:{num}` → `driverLaps:{num}` + `driverSectors:{num}` + `driverMiniSectors:{num}`
- `driverLastLap:{num}` → `driverLaps:{num}.lastLap`
- `driverLapTimes:{num}` {lap→time} → `driverLaps:{num}.laps` {n:{time,personalBest,overallBest}}
- `driverTyres:{num}` → `currentTyre:{num}` {compound,isNew,age} + `tyreHistory:{num}`
- `lapClassification:{num}` → `driverLapClassification:{num}` {lap,trackPct,type}
- `fiaStewards` {stack} → `driverPenalties:{num}` [..] (per-driver, [] clears)
- `raceControlMessages` (accumulate) → `raceControlMessage` (one per new; seek replays history)
- `championshipPrediction` → `championshipDrivers` + `championshipConstructors`
- `weatherData` PascalCase keys → camelCase (airTemp,trackTemp,pressure,humidity,rain,windSpeed,windDirection)
- `CarData.z` (raw) → `liveTelemetry:{num}` {dp,speed,rpm,gear,throttle,brake,ts,lap,lapElapsedMs}
- `lapTelemetry:{num}:{lap}` → `telemetryLap:{num}:{lap}`
- NEW: `driverDelta:{num}`, `fastestLap`, `driverFlag`, `driverSectors/MiniSectors`, `currentTyre/tyreHistory`
- DROPPED (no new emitter — confirm w/ user): `segmentLayout` (derive from miniSectors
  array lengths), `lapAffectedBy:{num}`, `telemetryEmpty`. `telemetryAvailable` still
  sent by session.py restore-extras.
- Unchanged: clock, trackStatus, trackCircuit, position, yellowFlag, trackGeometry,
  meetingName, sessionBadge, driverList, driverGap:{num}, driverInt:{num}.
