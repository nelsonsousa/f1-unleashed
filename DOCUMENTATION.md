# F1Unleashed — Documentation

A Formula 1 live-timing and replay application with synchronised audio commentary, multi-source analysis, and per-session deep dives.

**Release 1.0.0** — June 7, 2026, day of the 2016 Monaco Grand Prix. Celebrating Mclaren's 1000th Grand Prix and 60th anniversary of their first Grand Prix.

**Current release**: 2.0.0, 2026-07-18 — on the eve of the Belgian Grand Prix.

The server listens on port **1950**, an homage to the first F1 World Championship.

This document describes what the application does and how it's structured. For install
instructions see [README.md](README.md). For an end-user walkthrough, the in-app **user
guide** (served at `/help`, split into the main window + one page per session type) is the
place to start.

## Contents

- [Legal disclaimer](#legal-disclaimer)
- [What it does](#what-it-does)
- [The interface](#the-interface)
- [Data stream + visuals](#data-stream-visuals)
- [Audio stream](#audio-stream)
- [Team radio](#team-radio)
- [Status footer + data-health monitor](#status-footer-data-health-monitor)
- [Weather — current conditions + forecast](#weather-current-conditions-forecast)
- [Video sync](#video-sync)
- [Login process](#login-process)
- [Settings](#settings)
- [Caching](#caching)
- [Replays vs live](#replays-vs-live)
- [Architecture](#architecture)
- [Future developments](#future-developments)

---

## Legal disclaimer

This is a personal project, to improve my own experience while watching Formula 1. It's not intended to infringe on any organisation's copyright or trademarks. 

This project is intended for personal use only, and solely by persons legally allowed to stream and download live timing data and Formula 1 TV coverage.

While Formula 1's timing data is publicly available (with some limitations), it's still protected by copyright and its distribution is almost certainly a violation of copyright law in most jurisdictions.

Distribution of the processed data is therefore not allowed. Streaming of the client UI to others is nor permitted. Sharing of formula1.com credentials is a violation of Formula 1's usage policy. 


## What it does

F1Unleashed connects to the F1 SignalR feed (live) or replays cached session data (historic), runs pre-processing on the raw timing stream, and visualises everything in a browser. It captures broadcast audio in parallel, aligns it to the data stream, and ships a session-aware UI tuned per session type (Practice / Qualifying / Race).

Lap classification (= PUSH / COOL / LONG / OUT / IN / PIT / RACE / WET / STOP) is derived from a combination of telemetry data and lap-time deltas, with absolute-delta fallback when the telemetry feed has outages.

---

## The interface

A tour of what each part of the UI shows and does.

### Main page

![Main page](static/images/screenshots/main_page.png)

The landing page lists every Grand Prix weekend in the current season and the prior weekend's cached sessions, plus controls for login and live-capture status.

- **Race calendar** — one card per event. Past events are clickable and open the session popover; upcoming events appear faded.
- **Session popover** — opens beneath the event card and lists the FP / Q / Sprint / Race sessions for that weekend. Each session row offers **Download** (pull the raw F1 timing data for a finished session — one-shot, runs in the background), **Open** (launch the session view at speed 1×), and **Delete** (remove the cached session from disk).
- **Login button** — opens the browser-based F1 login. After login the token is stored at `~/Library/Application Support/fastf1/f1auth.json` for ~72 h.
- **Live-capture status** — shows when a live session is being captured automatically by the adaptive session monitor.
- **Footer** — the app name and version sit in the centre, a Help (?) icon on the left opens this documentation page, and a settings gear on the right opens the settings dialog (see [Settings](#settings)).

### Practice view

![Practice view](static/images/screenshots/practice.png)

Optimised for free practice: lots of timed-lap context, pace classification, tyre history, telemetry comparison.

- **Header** — local + session clock, track status, playback controls, audio controls.
- **Standings** — position, driver, lap type, best lap, gap to leader, mini-sectors, S1/S2/S3 times, last lap time, tyre history, number of laps.
- **Track map** — track SVG with the position of each driver, the Current Conditions weather panel, the rain-radar overlay, and a short-range weather forecast widget (In 15' / 30' / 60', with rain probability for wet slots).
- **Telemetry** — multi-driver SPD / RPM / GEAR / THR-BRK traces with a per-driver lap list. Can show the live trace, last lap, best lap, and a selection of laps for comparison; in qualifying a toggle groups the lap list by part (Q1/Q2/Q3). Corner labels along the x-axis match the circuit map.
- **Race control** — RC message stream (with team-radio clips interleaved by time) plus a **Team Radio** tab listing every clip. Each clip has Play / Stop buttons; playing a clip ducks the commentary for its duration and then restores it.

### Qualifying view

![Qualifying view](static/images/screenshots/qualifying.png)

Practice-like layout plus Q-specific features: knockout-zone indicator, lap-time prediction, and predicted qualifying pace per team.

- **Standings** — for drivers in the elimination zone, the gap is shown to the driver on the bubble. Only the current tyre is shown. During a qualifying attempt the driver's delta to their best lap is shown live with the positions it would gain; once the lap completes, the actual delta and positions gained are shown.
- **Pecking order** — a tab in the race-control tile shows the predicted ranking of teams and their gaps.

### Race view

![Race view](static/images/screenshots/race.png)

Optimised for the race: gaps to leader and to the car ahead, tyre history, penalties, and championship standings.

- **Standings** — like the Practice view but showing gaps to leader and to the car ahead. Also shows blue flags, penalties (under investigation and imposed), and black-and-white flags.
- **Race control** — tabs: **RCM** (live RC message stream with team-radio clips interleaved by time); **Team Radio** (every captured clip, each with Play / Stop; playing a clip ducks the commentary); **Pecking order** (pre-race predicted team rank and pace); **Championship** (provisional driver + constructor standings updated from the current order).

### Common controls

- **Scrubber** — drag to seek to any point in the session. Click an event marker to jump to ~60 s before that event. Marked events: 2' notice before the race; session start; session finished; safety car / virtual safety car; green flags; red flags.
- **LIVE button** (live sessions only) — replaces the speed button; red when at the live edge, black when behind. Click to snap to the latest available state.
- **Speed** — 1× during live; 1×–50× during replay.
- **Audio controls** — mute, volume, a **Delay** box (`ss.SSS`; manual fallback offset — positive plays the commentary later, negative earlier), and a traffic light (green = audio in sync; yellow = seeking / loading; red = no audio for the current data-clock position).
- **Status footer** — see [Status footer + data-health monitor](#status-footer-data-health-monitor).
- **Video sync** — align the data clock to a TV broadcast you're watching alongside; see [Video sync](#video-sync).
- **Player help** — a link on the right of the status footer opens a modal with the playback-control reference; it is a client-only overlay, so it does not pause playback.

### Dashboard view

The telemetry tile has a **Dashboard** toggle that swaps the multi-driver traces for a focused
**two-driver** view, tuned per session type:

- **Practice / Qualifying** — live gauges per driver plus a mini telemetry (speed-trace) viewer.
  In qualifying the stopwatch shows a **lap-time forecast** (label `FORECAST`) while a lap is
  running, switching to `LAP TIME` once the lap is confirmed.
- **Race** — a battle panel per driver (TLA, position, the interval between the pair, a pit
  indicator, tyre compound/age, and a close-gap highlight) plus a **zoomed, self-centring mini
  track-map** (`track_map.js` secondary SVG instance) that follows the chasing car.

**Auto-select** (`DashboardAutoSelectProcessor`, `dashAutoSelect` topic; on by default) picks
the two drivers most worth watching and re-picks as the session evolves: closest to finishing
a push lap (practice); the at-risk drivers on a push lap (Q1/Q2); predicted/current top-5 (Q3);
the frontmost close battle (race). A manual TLA click hands control back to the user (auto
off); the picker holds a changed pick for a few seconds of session time so a just-completed lap
can be read before switching. The two-driver panels are computed by `DashboardInfoProcessor`
(`dashInfo` topic) — server-computed, client-rendered, as everywhere else.

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
| Race control | RC message stream (with team-radio clips interleaved by time); a Team Radio tab; provisional championship standings |
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

Audio commentary from `rdio.formula1.com` is captured as HLS (`ffmpeg -c copy`), written to disk as `commentary.aac`, and stored alongside the session data. The browser plays it through **MediaSource Extensions**: the raw ADTS-AAC bytes are range-fetched and transmuxed to fMP4 in-browser (`static/js/lib/aac_fmp4.js`), so live and replay share one natively-seekable playback path.

**Sync rules:**
- **Byte-0 anchoring.** `audio_info.json:start_utc` is pinned to the broadcast `PROGRAM-DATE-TIME` (UTC) of the *exact first segment ffmpeg captured* — identified from ffmpeg's own log, so it is race-free regardless of when capture started. A background side-car (`app/services/audio_pdt_tracker.py`) reads the HLS playlist to establish and persist this anchor, identically for live and replay.
- The client maps the data clock to an audio position via that per-segment anchor (`clockToAudioSec`), so the two stay aligned across reconnects, HLS rolling-window drift, and ffmpeg jitter. A capture restart produces additional segments; the server serves them as one virtual concatenation so the mapping still holds.
- **Live-edge cap.** During live capture the data clock is held to whichever stream is lagging — `min(data_edge, audio_edge) − buffer` — where the audio edge is the *captured-file* edge (byte-0 anchor + the duration of the bytes ffmpeg has written), tracked in `pdt_map.jsonl`. This keeps audio available at the live tail; if the audio edge goes stale (a capture stall) the cap releases so the data clock keeps flowing.

The audio controls in the header are: a traffic light (sync state), mute, volume, and a **Delay** box (`ss.SSS`, ±) — a manual fallback offset, rarely needed now that the byte-0 anchor is automatic.

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

Align the data clock to a live TV broadcast you are watching alongside. It is **one-shot, on demand** — clicking **Video sync** briefly screen-shares the (muted) TV, runs OCR (Tesseract.js) over the shared frame, seeks the data once to match, then releases the capture (zero idle cost). It is layout-agnostic: it crops large black borders and sub-frames the region of interest rather than assuming a fixed position. Audio stays auto-synced to the data clock via the PDT anchor, so you only ever align the *data* clock to the TV moment.

- **P/Q (button)** — OCRs the on-screen session clock once and seeks the data to match (<1 s).
- **Race (button)** — captures the lap counter ~1×/s for ~10 s, finds the frame where it ticks up, and aligns that to the data's lap-cross. Click near a lap change. (It needs laps running, so this is the tool to use once the race is green.)

**Keyboard**

- **ENTER** (always available) — jump to a start instant, and resume playback if paused.
    - *P/Q*: the next GREEN flag (session start / restart).
    - *Race*: the **scheduled start** (= formation-lap start, when the analog Rolex hand hits the hour) if the clock is within the first minute after the scheduled time; otherwise **lights-out** (press as the five lights go out). The snap is start-phase only (≤ lap 1); after that ENTER just resumes.
- **`+` / `=`** — the TV is ahead: nudge the data forward ~0.5 s.
- **`−`** — the TV is behind: pause ~0.1 s so the picture catches up.

The `+` / `−` nudges become available once you have used the Video sync button at least once in the session.

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
- Audio follows the data clock automatically via the byte-0 PDT anchor; native (MSE) seeking lands audio together with the data.

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
| `TyreProcessor` | TimingAppData, driverStatus | `currentTyre:{num}` (compound, isNew, age) |
| `LapPredictionProcessor` | lapTelemetry, driverLastLap, lapClassification | `lapPrediction:{num}` (predicted lap time + predicted position, updated as a lap runs) |
| `PitStopLossProcessor` | driverStatus, driverGap/Int, tyres, trackStatus | `pitStopTimeLoss` (per in-race stop: stationary time, total loss, SC/VSC context, position change, rejoin traffic) |
| `DashboardInfoProcessor` | standings, driverInt, driverStatus, currentTyre, lapPrediction, lapClassification | `dashInfo` (per-driver two-driver-panel state: position, interval, indicators, tyre, lap-time label) |
| `DashboardAutoSelectProcessor` | position, standings, qualifyingPart, lapClassification, lapPrediction, driverInt, driverStatus, sessionInfo | `dashAutoSelect` ([num1, num2] — the recommended watch pair, per session type) |
| `ChampionshipProcessor` | ChampionshipPrediction | `championshipPrediction` |
| `TeamRadioProcessor` | TeamRadio | `teamRadio` (per-clip play event at its broadcast Utc) |
| `DataHealthProcessor` | TimingData, Position.z, CarData.z, Heartbeat, driverList, trackStatus, driverStatus | `dataHealth` (per-stream timing / telemetry / position health over on-track drivers) |

> The table lists the headline processors; the full chain (sector/timing/gap/pace
> decomposition, FIA stewards, best-sector, heartbeat, etc.) lives under
> `app/processing/processors/`, and post-session analysis (pecking order, pit-loss estimate &
> measurement, tyre phases, strategy) under `app/analysis/`.

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

## Future developments

Data analysis and predictions are the hardest part of this project. 

Not only is the available data very sparse, when compared to each teams own telemetry, but there are data outages on occasion (GPS failures, telemetry failures, timing data delays, etc.). 

But, as much as possible, I'll work to enrich the analysis of the data and provide what I hope is a better viewing experience for the Formula 1 fans.

### Delivered in v2.0

- **Live Dashboard view** — a two-driver focus on the telemetry tile: live gauges + lap-time
  forecast (practice/qualifying) and a battle panel + zoomed self-centring mini track-map
  (race). See [Dashboard view](#dashboard-view).
- **Auto-select** — server-computed recommendation of the two drivers most worth watching,
  re-picked per session type as the session evolves (`DashboardAutoSelectProcessor`).
- **Pecking-order predictor** — predicted team ranking and pace from practice and qualifying
  running (`app/analysis/pecking_order.py`).
- **Pit-stop measurement + time-loss** — per-stop stationary time, total time lost, SC/VSC
  context, position change and rejoin traffic, plus a pre-race pit-lane time-loss estimate
  (`PitStopLossProcessor`, `app/analysis/pit_loss_*`).
- **Split user guide + player help** — the in-app guide (`/help`) is now one page per context,
  and a **Player help** modal (status-footer link) documents the controls without pausing
  playback.

### Delivered in v1.3

- **Automatic audio sync** — commentary is anchored to the broadcast `PROGRAM-DATE-TIME` of ffmpeg's exact first captured segment, so it aligns to the data clock automatically; the manual **Delay** box is now just a fallback (see [Audio stream](#audio-stream)).
- **Unified live/replay audio** — MSE playback with the server serving multi-segment captures as one virtual concatenation, so a capture restart no longer breaks live audio and live behaves exactly like replay.
- **Robust live-edge cap** — the data clock is capped to the captured-file audio edge (audio stays available at the live tail) with a soft-couple stall-release so an audio hiccup no longer freezes the session.
- **Video-sync race anchoring** — ENTER snaps to the scheduled start or lights-out on a fixed pivot and resumes playback if paused (see [Video sync](#video-sync)).

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


