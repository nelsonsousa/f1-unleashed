# F1Unleashed

Download, replay, and analyse Formula 1 live-timing data, with synchronised
commentary audio and per-session analysis. Python/FastAPI backend, Jinja2 +
vanilla-JS modular frontend.

For what the app does, how it's built, and a tour of the UI, see
[README.md](README.md) and [DOCUMENTATION.md](DOCUMENTATION.md) (architecture +
processor reference + interface guide). Open work is tracked on the project's
Trello board (the "F1 Unleashed" board).

## Critical rules

1. **Handlers receive `(data, clockTime)`** — use `clockTime.getTime()` for all
   timing. Never use `Date.now()`.
2. **Only payload timestamps matter** — the only timestamp that counts is the one
   INSIDE the message payload (e.g. `Utc` in CarData.z entries, `Timestamp` in
   Position.z entries). Never use the envelope/recording timestamp for ordering
   or matching.
3. **Implement exactly what's asked** — no defensive fallbacks, no extra
   features, no invented requirements. Missing data is a bug to fix, not to hide.
4. **External CSS only** — no inline styles, one stylesheet per component. No
   emojis unless requested.
5. **Server computes, client renders** — display state (standings, track status,
   finished flags, championship, etc.) is computed in the processing pipeline and
   emitted as `display:*` / per-driver topics. The frontend should render what the
   server sends, not re-derive it.

## Architecture

```
F1 SignalR (live)  ─┐
                    ├─► SessionPreProcessor ─► transient DB ─► FastAPI (SSE) ─► browser message bus ─► tiles
live.jsonl (replay)─┘   (runs processors,      (scratch file in
                         snapshots for seek)     ./tmp, per session)
```

- **Capture/replay share the same processors.** Live capture and replay both
  feed `app/processing/processors/*`, so behaviour matches.
- **The processed DB is transient.** It is a scratch file under `./tmp`
  (one per event/session), built on demand from `live.jsonl` when a client
  connects and deleted on disconnect (kept in DEBUG mode). On connect any
  existing scratch DB is deleted and rebuilt, so replay always runs the latest
  processor code — there is no persisted `session.db` to reprocess.
- **Seek-safety:** each processor implements `snapshot()`/`restore()`; the player
  restores latest-state-per-topic on seek, so client tiles must not keep
  edge-triggered state that a seek can't reconstruct.

## Directory structure

### `app/` — backend (FastAPI)
| Path | Purpose |
|------|---------|
| `main.py` | App entry, lifespan, live-session monitor, startup DB backfill |
| `routers/` | `auth`, `livetiming` (CDN download), `livetiming_stream` (SSE + playback control), `races` (schedule), `downloads`, `weather`, `replay` |
| `services/signalr_client.py` | Live SignalR connection to F1 |
| `services/livetiming_fetcher.py` | Download historic data from livetiming.formula1.com |
| `services/live_capture.py` | Live capture orchestration (data + commentary audio) |
| `services/audio_sync.py`, `services/audio_pdt_tracker.py` | Commentary anchoring via HLS PROGRAM-DATE-TIME |
| `services/auth_service.py`, `cli/login.py` | F1 auth; browser-based login (API has bot detection) |
| `services/f1_service.py` | FastF1 wrapper for schedule/session metadata |
| `processing/preprocessor.py` | Builds `session.db` from `live.jsonl` (replay) or a live tail |
| `processing/message_bus.py` | Server-side topic bus (`on`/`emit`, `*` wildcard) |
| `processing/processors/` | One processor per concern — standings, track_status, session_data, championship, lap_classification, pace, telemetry, position, driver_list/status, race_control, fia_stewards, event_detector, clock, lap_prediction, … |
| `analysis/` | Post-session analysis: pecking_order, strategy_prediction, tyre_phases, pit_loss_measurement |

### `static/` — frontend
| Path | Purpose |
|------|---------|
| `js/base.js` | Client message bus + playback engine + snapshot/seek handling |
| `js/components/header.js`, `tv_sync.js` | Header (clocks, track status, playback/audio controls); TV-sync (coming soon) |
| `js/components/tiles/` | `standings.js` (practice/qualifying/race, driven by `SESSION_CONFIG.sessionType`), `track_map.js`, `telemetry.js`, `weather_radar.js`, `race_control.js` |
| `js/home.js`, `browser.js`, `replay.js` | Landing page, cache browser, replay page |
| `css/` | One stylesheet per component (external only) |
| `images/tracks/`, `images/tyres/` | Track SVGs + tyre-compound icons |

### `templates/` — Jinja2
`base.html` (injects `SESSION_CONFIG`), `components/` partials, `pages/session.html`
(one page parameterised by session type).

### `data/` — local cache (gitignored)
```
data/livetiming_cache/{year}/{NN_event}/{session}/
    live.jsonl       # one JSON message per line: {"Type","DateTime","Json"}
    subscribe.json   # initial state snapshot
data/analysis/{year}/{event}/{session}/*.json   # analysis outputs
tmp/{year}_{event}_{session}.db   # transient processed DB (built on demand, deleted on disconnect)
```

### `utils/track_generation/` — tooling
Track-SVG generation (`generate_track_svgs.py`, `fetch_circuit_data.py`,
`track_config.json`) — run rarely, only when a new circuit appears (e.g. Madrid
2026). Audio↔data anchoring is purely PDT-based: `app/services/audio_pdt_tracker.py`
pins the byte-0 PROGRAM-DATE-TIME anchor during capture (no cross-correlation /
credits detection — `audio_sync.py` was removed).

## Client message bus

```javascript
messageBus.on('TopicName', (data, clockTime) => {
    const messageTime = clockTime.getTime();   // use this, not Date.now()
});
```
Special playback events: `state:reset`, `state:restore` (latest state per topic on
connect/seek), `state:seek-complete`, `state:clock`, `state:status`.

Playback: UTC-based clock, ~2 s display delay for interpolation, periodic
snapshots for seeking, 1×–50× replay (1× live).

## Commands

```bash
./f1unleashed.sh start|stop|restart|status          # server on :1950
python -m app.cli.login                          # browser-based F1 login
```

Branching: `main` is the release branch.
