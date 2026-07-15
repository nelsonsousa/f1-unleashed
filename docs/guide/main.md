# User guide — the main window & player

F1 Unleashed lets you **replay** (or watch **live**) a Formula 1 session in your browser,
with synchronised commentary audio, team radio, weather, and per-session analysis. This
page covers everything that is the **same in every session**: the home page, and the
playback controls, audio, weather, video sync, status bar and settings that surround every
session view. For what each session type adds, see the **Practice**, **Qualifying** and
**Race** pages of this guide.

> New to the player controls mid-session? The status bar has a **Player help** link (bottom
> right) that opens the same control reference as a pop-up you can read while playback keeps
> running.

---

## The home page

![The home page — season calendar and cached sessions](/static/images/screenshots/main_page.png)

The landing page lists every Grand Prix weekend of the current season, plus the sessions
you already have cached.

- **Race calendar** — one card per event. Past events are clickable; upcoming events appear
  faded.
- **Session popover** — opens beneath an event card and lists that weekend's sessions
  (FP1–3 / Qualifying / Sprint / Race). Each row offers:
    - **Download** — pull the raw F1 timing data for a finished session (one-shot, runs in
      the background).
    - **Open** — launch the session view at 1× speed.
    - **Delete** — remove the cached session from disk.
- **Login** — opens the browser-based F1 login (see [Login](#login) below).
- **Live-capture status** — shows when a live session is being captured automatically.
- **Footer** — the app name and version sit in the centre; a **Help (?)** icon on the left
  opens this guide, and a **settings gear** on the right opens the settings dialog.

<!-- SCREENSHOT (new): the session popover expanded under an event card, showing the
     Download / Open / Delete rows. -->

---

## Live vs replay

The session view is the same whether you are watching live or replaying — only a few
controls differ.

| | **Live** | **Replay** |
|---|---|---|
| Starts | Automatically when a session goes live | When you click **Open** on a cached session |
| Speed | Locked to **1×** | **1×–50×** |
| Edge | A **LIVE** button snaps you to the latest data | — |
| Seeking | Rewind freely, then jump back to Live | Seek anywhere, instantly |
| Audio | Follows the data clock automatically | Follows the data clock automatically |

The app polls F1's schedule and **starts live capture on its own** — no action needed. When
a session is live you'll see it flagged on the home page.

---

## The player

Every session view shares the same frame: a **header** across the top, the session **tiles**
in the middle, and a slim **status bar** along the bottom.

### Header — clocks, track status, controls

![Session header](/static/images/screenshots/practice.png)

<!-- SCREENSHOT (new): a tight crop of just the header bar — clocks, track-status light,
     playback controls, audio controls. -->

- **Clocks** — your local time and the session clock.
- **Track status** — the current flag state (green / yellow / SC / VSC / red / chequered).
- **Playback controls** — play/pause, the scrubber, and speed (or the LIVE button).
- **Audio controls** — mute, volume, a manual **Delay** box, and the sync traffic light.

### Playback & seeking

- **Scrubber** — drag anywhere to seek. Seeks are **instant**: the full session state is
  rebuilt at the target moment, so you never wait for a re-play-up.
- **Event markers** — ticks on the scrubber. Click one to jump to **~60 s before** that
  event. Marked events: the 2-minute notice, session start, session finish, safety car /
  VSC, green flags and red flags.
- **Speed** — 1×–50× in replay; 1× live.
- **LIVE button** (live only) — red at the live edge, black when you've rewound. Click to
  snap back to the latest data.

### Audio — commentary & team radio

The broadcast commentary is captured and played back **in sync with the data automatically**,
across seeks and speed changes.

- **Mute / volume** — standard controls for the commentary.
- **Delay box** (`ss.SSS`, ±) — a manual offset, rarely needed now that audio is
  auto-anchored. Positive plays commentary later, negative earlier.
- **Sync traffic light** — green = in sync, yellow = seeking / loading, red = no audio for
  the current moment.
- **Team radio** — captured driver radio clips appear in the Race control tile (see the
  session pages). Playing a clip briefly **ducks** the commentary, then restores it.

---

## Weather

![Weather tile with current conditions and forecast](/static/images/screenshots/qualifying.png)

The **Current Conditions** tile combines three sources:

- a **sky-condition icon** (Open-Meteo), indexed by the playback clock;
- **live measurements** — air and track temperature, humidity, pressure and wind, from F1's
  own weather feed;
- a **rain-radar overlay** showing precipitation moving across the circuit.

A **Weather Forecast** widget in the corner shows the **In 15' / 30' / 60'** outlook with a
rain probability for wet slots. Forecasts are captured live so a replay shows exactly what
was predicted at the time.

<!-- SCREENSHOT (new — rain radar): capture a few radar frames from a region where it is
     actually raining and the precipitation is visibly MOVING, then overlay them onto a past
     (dry) circuit so the animation of rain sweeping across the track is visible. -->

---

## Video sync — line the data up with a TV broadcast

If you're watching the TV broadcast alongside the app, **Video sync** aligns the data clock
to the picture. It is one-shot and on demand — it briefly screen-shares your muted TV, reads
the on-screen clock or lap counter, seeks the data once to match, and releases the capture.

- **P/Q** — reads the on-screen session clock and seeks to match.
- **Race** — watches the lap counter for a few seconds and aligns to the lap change (use once
  the race is green; click near a lap change).
- **Enter** — jump to the start instant (next green in P/Q; scheduled start or lights-out in
  the race) and resume if paused.
- **`+` / `−`** — fine nudges once you've used Video sync: `+` if the TV is ahead, `−` if it's
  behind.

Audio stays locked to the data clock throughout, so you only ever align the *data* to the TV.

---

## The status bar

![Status bar](/static/images/screenshots/race.png)

<!-- SCREENSHOT (new): a tight crop of the bottom status bar, ideally during a live session
     so the download-speed items and the data-health boxes are visible. -->

The slim bar along the bottom reports the health of the stream:

- **Mode** — Live or Replay.
- **Stream / Messages** — throughput (msg/s) with a health light, and total messages so far.
- **Cache / Audio** — this session's on-disk size and the commentary bitrate.
- **Data buf / Audio buf** — how much data and audio are buffered ahead of the playhead.
- **Data health** — three boxes, **TIMING / TELEMETRY / POSITION**, covering the cars
  currently on track: green = good, yellow/orange = minor loss, red = outage. They pause
  during red / SC / VSC, when a data pause is legitimate.
- **Player help** — opens the playback-controls pop-up (readable while playing).

---

## Settings

![Settings dialog](/static/images/screenshots/settings.png)

The settings gear on the **home-page footer** opens the settings dialog. Highlights:

- **Cache location** — where captured sessions are stored (a native folder picker; offers to
  move your existing cache).
- **Per-session capture toggles** — download & play commentary, download team radio, and keep
  downloaded files, set independently for practice / qualifying / race.
- **Team-radio autoplay** — play radio clips automatically during replay (off by default).
- **Rain-radar key** — the precipitation-overlay API key.
- **Notifications** — a webhook (e.g. ntfy) for session-live / pre-session / token-expiry
  alerts, plus favourite drivers and teams.

Everything has a sensible default, so the app runs out of the box.

---

## Login

Live feeds, premium audio and full telemetry require a **formula1.com** subscription.

- Login is **browser-based** — click **Login** on the home page (or run
  `python -m app.cli.login`).
- The token lasts about 72 hours; the app warns you before it expires.

---

## Caching

Each session you capture or download is stored on disk (the location is configurable in
Settings). Replays are built on demand from that cache and are fully seekable. Deleting a
session from the home page removes it from disk.

---

## Support the project

F1 Unleashed is a free, personal project built to make watching Formula 1 better. If it
improves your race weekends, you can
[buy me a coffee](https://www.buymeacoffee.com/nsousa). <!-- TODO: confirm handle -->
