# F1Unleashed — Documentation

A Formula 1 live-timing and replay application with synchronised audio commentary, multi-source analysis, and per-session deep dives.

**Release 1.0.0** — June 7, 2026, day of the 2016 Monaco Grand Prix. Celebrating Mclaren's 1000th Grand Prix and 60th anniversary of their first Grand Prix.

The server listens on port **1950**, an homage to the first F1 World Championship.

This document describes what the application does and how it's structured. For install instructions see [README.md](README.md).

---

## Legal disclaimer

This is a personal project, to improve my own experience while watching Formula 1. It's not intended to infringe on any organisation's copyright or trademarks. 

This project is intended for personal use only, and solely by persons legally allowed to stream and download live timing data and Formula 1 TV coverage.

While Formula 1's timing data is publicly available (with some limitations), it's still protected by copyright and its distribution is almost certainly a violation of copyright law in most jurisdictions.

Distribution of the processed data is therefore not allowed. Streaming of the client UI to others is nor permitted. Sharing of formula1.com credentials is a violation of Formula 1's usage policy. 


## What it does

F1Unleashed connects to the F1 SignalR feed (live) or replays cached session data (historic), runs pre-processing on the raw timing stream, and visualises everything in a browser. It captures broadcast audio in parallel, aligns it to the data stream, and ships a session-aware UI tuned per session type (Practice / Qualifying / Race).

Race and qualifying pace analysis is performed after each FP session to estimate pecking order. Lap classification (= PUSH / COOL / LONG / OUT / IN / PIT / RACE / WET / STOP) is derived from a combination of telemetry data and lap-time deltas, with absolute-delta fallback when the telemetry feed has outages. Predictions (lap times in qualifying, pace through race stints) update as new data arrives.

---

## Data stream + visuals

The data stream is a sequence of typed messages on a server-side message bus, replayed from a SQLite cache or streamed live.

**Visuals** 

| Tile | What it shows |
|------|----------------|
| Header | Local time, session clock, track status, playback controls, audio controls (= mute, volume, sync indicator) |
| Standings | Position; time gaps; penalty + flag indicators (R); timing sectors; lap classifications; tyre history, etc. |
| Track map | Circuit SVG with per-driver positions, yellow-flag sector overlays, Current Conditions weather, rain radar overlay, and a short-range weather forecast widget |
| Telemetry | Speed / RPM / gear / throttle / brake / DRS traces with lap selection, multi-driver compare, lap history |
| Race control | RC message stream (with team-radio clips interleaved by time); a Team Radio tab; predicted team pecking-order; provisional championship standings |
| Status footer | A slim bar at the bottom of the player: live/replay indicator, stream throughput (msg/s), total messages, on-disk cache size, audio bitrate, live download speeds, and the data-health monitor (timing / telemetry / position) |

The frontend listens via a message bus pattern:

```javascript
messageBus.on('TopicName', (data, clockTime) => {
    const t = clockTime.getTime();  // single source of time
});
```

All clock-relative computations use the message's payload timestamp for faithful replay and skip forwards/backwards.

---

## Audio stream

Audio commentary from `rdio.formula1.com` is captured as HLS, written to disk as `commentary.aac`, and stored alongside the session data. The browser plays it through an HTMLAudioElement.

**Sync rules:**
- The capture-time audio anchor (`audio_info.json:start_utc`) is continuously re-derived from the HLS playlist's `PROGRAM-DATE-TIME` tag (= the broadcast wall-clock UTC of each segment) by a small background side-car (`app/services/audio_pdt_tracker.py`).
- On the frontend, audio plays at the position matching the data clock: `audio.currentTime = (clockTime − audio_start_utc) / 1000`. Because both anchors are broadcast UTC, the alignment holds across reconnects, HLS rolling-window drift, and ffmpeg timing jitter.
- A `pdt_map.jsonl` audit trail records every side-car observation (= wall-clock time, audio file duration, edge segment PDT) for post-session debugging.

The audio controls in the header are: traffic light (= sync state), mute, volume. No manual offset, no output-device picker — the PDT anchor is the single source of truth.

During live capture a watchdog restarts the commentary ffmpeg process if its HLS download stalls (= the output file stops growing). This is distinct from the silence-based watchdog that detects the end of a session.

---

## Team radio

F1 `TeamRadio` messages carry `Captures` of `{Utc, RacingNumber, Path}`, where `Path` points at an mp3 on the livetiming CDN. During live capture the clips are downloaded and cached to `{session}/TeamRadio/*.mp3` (existing sessions can be backfilled from `live.jsonl`).

- `TeamRadioProcessor` emits a `teamRadio` topic (`{num, file, utc}`) for each clip as it airs live, at the clip's broadcast `Utc`. The pre-session backlog carried on the initial subscribe is downloaded but not emitted for playback.
- In the race-control tile a **Team Radio** tab (between Race control and Pecking order) lists every clip; clips are also interleaved into the Race control message stream by time. Each entry shows an audio icon, the driver TLA, "Team radio", and Play / Stop buttons.
- Playing a clip **ducks** the commentary (mutes it for the duration of the clip, then restores it). Auto-play when a clip airs during replay is settings-gated (default off); otherwise clips play on demand via the Play button.
- Clips are served by `GET /api/v1/livetiming/teamradio/{session}/{file}`.

Transcription is not implemented (deferred).

---

## Status footer + data-health monitor

A slim status bar (= about half the header height) sits at the bottom of the session/player window. It shows the live/replay indicator, stream throughput (msg/s) with a traffic light, total messages, on-disk cache size, the commentary audio bitrate, and — for live sessions only — the data and audio download speeds.

It also hosts the **data-health monitor**: three coloured boxes — TIMING, TELEMETRY, POSITION — driven by the server-side `DataHealthProcessor` (`dataHealth` topic). Only drivers currently **on track** count (status TRACK / OUT; RET / STOP / PIT / FINISHED / DSQ are excluded, since a parked or retired car legitimately stops sending data).

- **TIMING** is all-or-nothing: red only if the whole `TimingData` feed has stopped (any `TimingData` arriving = green); it underpins everything else.
- **TELEMETRY** and **POSITION** are coloured by the fraction of on-track drivers affected: >50% red, >25–50% orange, >0–25% yellow, none green. Telemetry counts as "invalid" when throttle/brake exceed 100 or speed is 0 while the car is position-tracked, and "missing" when no recent `CarData` has arrived; position counts as stale when `Position` updates stop.
- Assessment is **green-gated**: red / SC / VSC pause the data legitimately, and a short grace window after green resumes lets the streams catch up before any stream is flagged.

---

## Weather — current conditions + forecast

The weather tile header is **Current Conditions**. The data is drawn from three sources:

- **Sky-condition icon** — from Open-Meteo (`/api/v1/weather`, hourly `weather_code` + `is_day`), indexed by the playback clock.
- **Live measurements** — temperature, track temperature, humidity, pressure and wind come from the F1 `WeatherData` feed.
- **Rain radar** — the precipitation overlay only; Rainbow.ai is used solely for the radar imagery, not the condition icon.

A **Weather Forecast** widget overlays the top-right of the radar/weather tile, showing the In 15' / 30' / 60' forecast condition icons (collapsing to current + 60' when the condition is unchanged across the window) and a rain probability (%) for wet slots.

The forecast is **captured live**: `ForecastCapture` (`app/services/weather_forecast.py`) fetches the Open-Meteo `minutely_15` forecast (`weather_code` + `precipitation_probability`) every 10 minutes during a live session and appends snapshots to `{session}/weather_forecast.jsonl`. Because Open-Meteo does not archive past forecasts, capturing live is the only way to replay what was predicted. Replay reads the snapshots via `GET /api/v1/weather/forecast?session=…` and indexes them by the playback clock.

---

## Video sync

Sync the data clock to a live TV broadcast you are watching alongside. Screen-share the (muted) TV window; the app runs OCR (Tesseract.js) over the shared frame to read the broadcast's session clock / countdown — layout-agnostic (it crops large black borders and sub-frames the region of interest rather than assuming a fixed position) — compares it to the data clock, and nudges playback to match. `ENTER` jumps to the next relevant moment for the session type (Practice / Qualifying: next start or restart; Race: formation lap, then lights-out, then tracking by lap number); `+` skips forward and `-` pauses briefly to let the picture catch up. It does not re-sync while the observed gap is under ~0.5 s, but always accepts a manual keypress. Audio stays auto-sync'd to the data clock via PDT, so you only ever align the data clock to the TV moment.

---

## Login process

Access to non-public F1 data (live session feeds, premium audio, telemetry) requires a `formula1.com` subscription and login token.

- Login is **browser-based only** (`python -m app.cli.login` or the login button on the homepage launches `pywebview`).
- Tokens are stored at `~/Library/Application Support/fastf1/f1auth.json` and last about 72 hours.
- The app monitors token expiry. If the token expires within 24 hours **and** the next session starts within 6 hours before expiry, a notification is sent via the configured webhook (= e.g. ntfy).

---

## Settings

All runtime configuration lives in a single JSON store, `settings.json`, under the OS data home (`app/settings.py`). `.env` and `python-dotenv` are gone — every value has a default, so the app runs out of the box, and the store is edited via an in-app **settings dialog** reached from the gear on the **home-page footer** (right side) only — not the session window.

Settings cover:

- **debug** — keep transient/ephemeral artefacts instead of deleting them.
- **cacheDir** — the livetiming-cache location (see below).
- **rainbowAiApiKey** — the precipitation-radar overlay key.
- **Per-session-type capture toggles** (practice / qualifying / race): download + play commentary audio; download team radio; keep downloaded files after the session.
- **teamRadioAutoplay** — auto-play radio clips during replay (default off; on-demand otherwise).
- **ntfy** — webhook URL plus which notifications to send (session-live / pre-session / token-expiry) and the pre-session lead time in minutes.
- **alerts** — favourite drivers (TLAs / car numbers) and teams (short names, case-insensitive).
- **auth** — token-expiry warning hours + check interval.

The `cacheDir` setting points **directly** at the livetiming-cache root: the chosen folder holds the season directories (`2026/`, `2025/`, …) with no extra `livetiming_cache` level. Everything else — `settings.json`, `known_topics.json`, `rainbow_usage.json`, `tmp`, `analysis`, and the weather-radar cache — stays at the fixed OS data home. Changing the cache location offers to **move** the existing cache and requires a restart; a native folder-picker is provided.

The settings API: `GET`/`PUT /api/v1/settings`, `POST /api/v1/settings/pick-folder` (native folder picker), `POST /api/v1/settings/cache-location` (relocate + move).

---

## Caching

Every captured session is stored on disk under an OS-appropriate data directory
— Windows `%LOCALAPPDATA%\F1Unleashed`, macOS `~/Library/Application Support/
F1Unleashed`, Linux `$XDG_DATA_HOME/f1unleashed`. The livetiming cache can be
redirected elsewhere with the `cacheDir` setting (see Settings); the rest of the
data home is fixed.

```
{cache-dir}/{year}/{NN_event}/{session_type}/
    live.jsonl              # one JSON message per line, payload-timestamp-ordered
    subscribe.json          # initial state snapshot at SignalR connect
    commentary.aac          # transcoded audio
    audio_info.json         # audio-clock anchor
    TeamRadio/*.mp3         # captured team-radio clips
    weather_forecast.jsonl  # 15-min forecast snapshots captured live (for replay)
```

Pre-processing reads `live.jsonl` once (or streams live), runs the processor chain, and builds a transient pre-processed SQLite DB under `{data-dir}/tmp/` (one per session, built on demand and removed on disconnect). Formula 1 timing messages only contain changes to previous data and as such make it hard to skip playback forwards/backwards. This pre-processing step makes every message history aware, and that allows near instant playback skip.

The data directory holds:

* **livetiming_cache**: Formula 1's streamed data and audio
* **weather_radar_cache**: precipitation radar images to re-use on replays
* **analysis**: supplemental data produced by the backend processing
* **tmp**: transient per-session pre-processed DBs

---

## Replays vs live

**Live:**
- Triggered automatically when a live session is active.
- SignalR connection writes to `live.jsonl` in append mode.
- The client streams via Server-Sent Events.
- Speed control is locked to 1×.
- "Live" indicator pinned to the latest data; user can rewind freely and then skip forwards to Live.

**Replay:**
- Loads processed data for the chosen cached session.
- SSE replays messages at adjustable speed (1× – 50×).
- Seeking lands on requested timestamp.
- Audio playback follows the data clock by default; offsets persist across seeks.

The adaptive live-session monitor polls F1's API at intervals depending on time-to-next-session (= 60 min when > 2 h away, 5 min when 1–2 h away, 60 s when < 1 h away) so live capture starts automatically with no user action.

---

## Architecture

### Data flow

```
F1 SignalR (live)   ──→ F1SignalRClient    ──→ live.jsonl (disk)
F1 CDN (historical) ──→ LiveTimingFetcher  ──→ live.jsonl (disk)
                                                  │
                                         SessionPreProcessor
                                        (reads JSONL, runs processors,
                                         writes session.db)
                                                  │
                                             session.db
                                          (SQLite: messages table
                                           indexed by topic + offset_ms)
                                                  │
                                            SessionEngine
                                         (DB-driven playback,
                                          instant seeking via
                                          DB lookups)
                                                  │
                                           WebSocket clients
                                          (browser components)
```

### Key components

**Data acquisition**

| Component | File | Purpose |
|-----------|------|---------|
| `F1SignalRClient` | `app/services/signalr_client.py` | Live SignalR connection; writes messages to `live.jsonl` |
| `LiveTimingFetcher` | `app/services/livetiming_fetcher.py` | Downloads historical sessions from F1 CDN |
| `LiveCaptureService` | `app/services/live_capture.py` | Live capture lifecycle; audio HLS → AAC via ffmpeg |
| Live Session Monitor | `app/main.py` | Adaptive poll of F1 API; auto-starts capture |

**Pre-processing pipeline**

| Component | File | Purpose |
|-----------|------|---------|
| `SessionPreProcessor` | `app/processing/preprocessor.py` | Main pipeline: reads JSONL, gates on SessionInfo, filters stale data, runs processors, writes session.db |
| `SessionDatabase` | `app/processing/database.py` | SQLite per session: `messages` (offset_ms, topic, data JSON) + `processing_meta` |
| `FileReader` | `app/processing/file_reader.py` | Reads JSONL with decompression + reorder buffer + tail-follow for live |
| `SessionMessageBus` | `app/processing/message_bus.py` | Python pub/sub between processors |


**Processors**

Each processor subscribes to raw F1 topics and emits processed messages. Per-driver messages use the `topic:driverNum` format.

| Processor | Subscribes to | Emits |
|-----------|----------------|-------|
| `SessionInfoProcessor` | SessionInfo | `sessionInfo` (type, name, status, gmtOffset, meetingName) |
| `SessionDataProcessor` | SessionData | `event` (Started/Finished), `sessionStatus` (Started/Aborted/Finished/Finalised), `sessionInfo` (qualifyingPart) |
| `ClockProcessor` | ExtrapolatedClock, SessionInfo | `clock` (utc, sessionTime, clockStatus) |
| `DriverListProcessor` | DriverList | `driverList`, `standings` |
| `DriverStatusProcessor` | TimingData, DriverList | `driverStatus:{num}` (PIT/OUT/TRACK/RET/STOP) |
| `TimingProcessor` | TimingData, TimingAppData | `driverGap:{num}`, `driverInt:{num}`, `driverTiming:{num}`, `driverTyres:{num}` |
| `RaceControlProcessor` | RaceControlMessages | `raceControlMessages`, `yellowFlag`, `driverFlag` |
| `TrackStatusProcessor` | TrackStatus, RaceControlMessages, sessionStatus (race) | `trackStatus` (race GREEN driven by `SessionStatus=Started`) |
| `WeatherProcessor` | WeatherData | `weatherData` |
| `PositionProcessor` | Position.z, SessionInfo | `trackGeometry`, `position` (all cars: x, y, distPct) |
| `TelemetryProcessor` | CarData.z, position, driverStatus | `lapTelemetry:{num}` (DB), `~telemetry:{num}` (live only) |
| `LapClassificationProcessor` | driverLastLap, driverStatus, etc. | `lapClassification:{num}` (PUSH/COOL/OUT/IN/LONG/RACE/WET/PIT/STOP) |
| `PaceProcessor` | driverLastLap, lapClassification, tyres | `pace.json` post-session (per-driver and per-team quali + race pace) |
| `ChampionshipProcessor` | ChampionshipPrediction | `championshipPrediction` |
| `TeamRadioProcessor` | TeamRadio | `teamRadio` (per-clip play event at its broadcast Utc) |
| `DataHealthProcessor` | TimingData, Position.z, CarData.z, Heartbeat, driverList, trackStatus, driverStatus | `dataHealth` (per-stream timing / telemetry / position health over on-track drivers) |

**Playback engine**

| Component | File | Purpose |
|-----------|------|---------|
| `SessionEngine` | `app/processing/session.py` | DB-driven playback: streams messages at clock rate; instant seek via `get_state_at()` |
| `SessionManager` | `app/processing/session.py` | Global singleton managing `SessionEngine` instances |
| `PlaybackClock` | `app/processing/clock.py` | Server-side clock with speed control + display delay |
| WS Router | `app/routers/livetiming_stream.py` | `WS /ws/{name}` endpoint |

**Seeking**

Instant seek via a single SQL query (latest message per topic at target offset):

```sql
SELECT topic, data FROM messages
WHERE rowid IN (
    SELECT MAX(rowid) FROM messages
    WHERE offset_ms <= ?
    GROUP BY topic
)
```

~20–40 ms for a full state restore across ~138 topics.

### On-disk cache format

```
{cache-dir}/{year}/{MeetingKey}_{Location}/{SessionKey}_{SessionName}/
    live.jsonl              # Raw F1 messages: {"Type": "...", "DateTime": "...", "Json": {...}}
    subscribe.json         # Initial state snapshot from SignalR subscription
    commentary.aac         # Audio recording (= live capture only)
    audio_info.json        # Audio anchor metadata (= live capture only)
    TeamRadio/*.mp3        # Team-radio clips (= live capture / backfill)
    weather_forecast.jsonl # 15-min forecast snapshots (= live capture only)
# the pre-processed DB is transient — built on demand under {data-dir}/tmp/
```

**session.db schema**

```sql
CREATE TABLE messages (
    offset_ms    INTEGER NOT NULL,           -- ms from session start (= playback clock)
    wall_clock   TEXT,                       -- HH:MM:SS.mmm at emission, for human-readable cross-reference
    topic        TEXT NOT NULL,
    data         TEXT NOT NULL               -- JSON
);
CREATE INDEX idx_msg_topic_offset ON messages (topic, offset_ms);

-- Per-lap telemetry sample arrays. Each row is one lap for one driver;
-- `data` is a JSON array of samples [distPct, speed, rpm, gear, throttle,
-- brake, t_ms_rel] where t_ms_rel is offset from lap start.
CREATE TABLE telemetry (
    driver           TEXT NOT NULL,
    lap              INTEGER NOT NULL,
    offset_ms        INTEGER NOT NULL,       -- lap-end offset on the session clock
    start_wall_clock TEXT,                   -- lap-start HH:MM:SS.mmm
    end_wall_clock   TEXT,                   -- lap-end HH:MM:SS.mmm
    data             TEXT NOT NULL,          -- JSON [[dp,s,r,g,t,b,t_ms_rel], ...]
    PRIMARY KEY (driver, lap)
);

CREATE TABLE processing_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

**Topic naming convention**

- Global topics: `trackStatus`, `weatherData`, `clock`, `standings`, etc.
- Per-driver topics: `driverTiming:44`, `driverGap:1`, `driverStatus:63`, `lapClassification:16`, etc.
- Live-only (= not saved to DB): `~telemetry:44` is emitted during playback but not persisted.

**Topic catalog (`known_topics.json`)**

`known_topics.json` (at the fixed data home) is a per-topic catalog. For each topic it records:

| Field | Meaning |
|-------|---------|
| `status` | `subscribed` (a processor handles it) / `received` (arrived but unhandled) / `unseen` (known baseline, absent this session) |
| `listeners` | the processor classes that subscribe to it (from the message-bus handler map) |
| `outputs` | the topics that processing produces from it (derived at runtime — a re-entrant emit inside a handler is recorded as an output of the current input topic) |
| `captured` | whether it is a raw F1 topic persisted to `live.jsonl` |
| `lastSeen` | the most-recent session it appeared in |
| `note` | a user-editable note, preserved across runs |

The existing topic-discovery alert is unchanged: when a genuinely new topic arrives that no processor handles, the app warns and sends a developer notification.

**Static-asset cache-busting**

Static asset URLs in templates are versioned automatically by file mtime via a Jinja `asset()` helper (`/static/<path>?v=<mtime>`) — there are no hand-bumped `?v=` tags. `index.html` is a Jinja template.

**CarData.z channel mapping**

| Channel | Value |
|---------|-------|
| 0 | RPM |
| 2 | Speed (km/h) |
| 3 | Gear |
| 4 | Throttle (0-100) |
| 5 | Brake (0-100) |

Telemetry data is streamed at roughly 3-4Hz. Position data is also streamed at roughly same frequency and these two samples are mapped together to assign a track position to each telemetry sample.

### Post-session analysis outputs

After preprocessing finishes, the analysis pipeline writes JSON files into `{data-dir}/analysis/{year}/{event}/{session}/`:

| File | Source | Used by |
|------|--------|---------|
| `pace.json` | `app/processing/processors/pace_processor.py` | `pecking_order.py` |
| `tyre_phases.json` | `app/analysis/tyre_phases.py` | Tyre + race analysis |
| `pecking_order.json` | `app/analysis/pecking_order.py` | Subsequent sessions + UI race-control tile |
| `strategy_prediction.json` (qualifying only) | `app/analysis/strategy_prediction.py` | Race tile predictions |
| `strategy_validation.json` (race only) | `app/analysis/strategy_validation.py` | Strategy retrospective |

The pecking-order chain runs within an event (FP1 → FP2 → FP3 → Q → R). Each pecking_order.json is ranked by pure pace gap to the predicted leader; no inertia from prior events' rankings, so a midfielder showing leader-pace ranks at the top immediately.

## Future developments

Data analysis and predictions are the hardest part of this project. 

Not only is the available data very sparse, when compared to each teams own telemetry, but there are data outages on occasion (GPS failures, telemetry failures, timing data delays, etc.). 

But, as much as possible, I'll work to enrich the analysis of the data and provide what I hope is a better viewing experience for the Formula 1 fans.

### Delivered in v1.2

- **In-app settings** — JSON-backed settings dialog replacing `.env` (see Settings).
- **Team radio replay** — clip capture + time-aligned playback with commentary ducking (see Team radio).
- **Status footer + data-health monitor** — bottom status bar with stream/data-quality indicators (see Status footer + data-health monitor).
- **Weather forecast** — live-captured 15/30/60-minute forecast widget (see Weather — current conditions + forecast).

### Planned features

- **Session summary / highlights** (= post-session recap: fastest lap, longest stint, biggest gap closes, position changes, podium).
- **Lift-and-coast** detection.
- **Tyre-saving** detection.
- **Pit windows** (SC / VSC opportunity detection).
- **Pit-strategy** predictions and simulations.
- **Dry/wet** tyre crossover identification.


