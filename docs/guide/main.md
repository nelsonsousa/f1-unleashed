# User guide — the main window & player

F1 Unleashed lets you **replay** (or watch **live**) a Formula 1 session in your browser,
with synchronised commentary audio, team radio, weather, and per-session analysis. This
page covers everything that is the **same in every session**: the home page, and the
playback controls, audio, weather, TV sync, status bar and settings that surround every
session view. For what each session type adds, see the **Practice**, **Qualifying** and
**Race** pages of this guide.

> New to the player controls mid-session? The status bar has a **Player help** link (bottom
> right) that opens the same control reference as a pop-up you can read while playback keeps
> running.

---

## Getting started — install & first run

You need **Python 3.13**, **ffmpeg** (with `ffprobe`) on your `PATH`, and a **formula1.com**
subscription for live sessions and full data.

```bash
git clone <repo-url> f1unleashed && cd f1unleashed
python3.13 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
./f1unleashed.sh start                 # then open http://localhost:1950
```

There's no config file to edit — everything has a default, so it runs straight away. Then, in
order:

1. **Log in.** Click **Login** on the home page (or run `python -m app.cli.login`). A browser
   window handles the F1 login; the token lasts about 72 hours.
2. **Set your options** in the settings dialog (gear, home-page footer). The two most worth
   setting up front: the **rain-radar key** (for the weather radar) and, if you want alerts, a
   **notifications webhook**. See [Settings](#settings) below for the rest.
3. **Get a session.** Open a past event on the calendar, pick a session, click **Download**,
   then **Open** to replay it. Live sessions are captured automatically.

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
| Speed | Locked to **1×** | **1×–10×** (1× / 2× / 5× / 10×) |
| Edge | A **LIVE** button snaps you to the latest data | — |
| Seeking | Rewind freely, then jump back to Live | Seek anywhere, instantly |
| Audio | Follows the data clock automatically | Follows the data clock automatically |

The app polls F1's schedule and **starts live capture on its own** — no action needed. When
a session is live you'll see it flagged on the home page. When a newer version is available,
the home page shows an **update hint**.

> **Handy interactions:** click a **car on the track map** or a **standings row** to focus that
> driver in the Dashboard; click a **sector header** in standings to swap `S{n}` for its
> best-sector `BS{n}`. Commentary volume is remembered between sessions (each session opens
> muted).

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
  event. Clickable event ticks: the 2-minute notice, session start, session finish, safety car /
  VSC, green flags and red flags. (Flag states — green / red / SC / VSC / chequered — are
  colour-highlighted; the others, like the 2-minute notice, are plain ticks.)
- **Speed** — 1×–10× in replay (cycles 1× / 2× / 5× / 10×); 1× live.
- **LIVE button** (live only) — red at the live edge, black when you've rewound. Click to
  snap back to the latest data.
- **Keyboard** — **Space** play/pause, **← / →** skip 10 s back/forward, **M** mute. (The
  SYNC TO / fine-nudge keys are under [Sync to a TV broadcast](#sync-to-a-tv-broadcast).)

### Audio — commentary & team radio

The broadcast commentary is captured and played back **in sync with the data automatically**,
across seeks and speed changes.

- **Mute / volume** — standard controls for the commentary.
- **Delay box** (`ss.SSS`, ±) — a manual offset, rarely needed now that audio is
  auto-anchored. Positive plays commentary later, negative earlier.
- **Sync traffic light** — green = in sync, yellow = seeking / loading, red = audio expected
  but not ready (a genuine content gap — before/after the recording, or between segments —
  shows no light instead).
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

## Sync to a TV broadcast

If you're watching the TV broadcast alongside the app, you can line the data up with the
picture. There's no screen-sharing — you seek to a shared reference point the TV also shows,
then fine-tune. Audio stays locked to the data clock throughout, so you only ever move the
*data*.

- **SYNC TO** (header button) — seeks to the previous reference marker: a whole **clock
  minute** in practice/qualifying, or the current **lap start** in the race (the **Lap 1**
  marker jumps to lights-out). A small label shows the mode and target; the button greys out
  when its marker is ahead of the playhead.
- **Enter** — jump to the SYNC TO marker and resume if paused.
- **`←` / `→`** — skip 10 s back / forward.
- **`+` / `−`** — fine nudges: `+` if the TV is ahead (data forward ~0.5 s), `−` if it's
  behind (pause ~0.1 s so the picture catches up).

### How to sync

- **Practice / Qualifying** — sync to an exact minute (the track clock or session clock) by
  pressing **Enter**. The app needs to be running **ahead** of the TV, so it can snap back to
  that minute as the TV reaches it.
- **Race** — get a **rough** sync before the race starts (snap to a whole minute), then an
  **exact** sync at the **start of the formation lap** (the app must be ahead of the TV to
  snap-sync). At **lights-out** you can get a near-perfect sync by snap-syncing the instant the
  five red lights go out.
- **Fine-tuning** — even at best, perfect sync is very hard. Use the **`+` / `−`** keys for
  sub-second accuracy, so the engine sounds match what's showing on the video.

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

The settings gear on the **home-page footer** opens the settings dialog. Everything has a
sensible default, so the app runs out of the box — change these when you want to:

- **Rain-radar key** — paste a (free) Rainbow.ai API key to switch on the precipitation
  overlay. Leave it blank and everything works except the rain radar.
- **Cache location** — where captured/downloaded sessions are stored. Point it at a roomy or
  external drive if you'll keep a lot; changing it offers to move your existing cache and needs
  a restart.
- **Per-session capture toggles** (practice / qualifying / race) — for each session type,
  whether to **download & play commentary**, **download team radio**, and **keep downloaded
  files** afterwards. Turn things off to save disk or skip audio for a type.
- **Team-radio autoplay** — play radio clips automatically as they air during replay (off by
  default; otherwise play them on demand from the Team Radio tab).
- **Notifications** — a webhook URL (ntfy / Discord / Slack) plus which alerts to send
  (**session-live**, **pre-session** with a lead time, **token-expiry**). Add **favourite
  drivers** (TLAs or numbers) and **teams** to highlight them.
- **Token-expiry warning** — how many hours before the F1 login expires you're warned.

Only the rain-radar key and (optionally) the notifications webhook usually need a value; the
rest are on/off preferences.

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
[buy me a coffee](https://buymeacoffee.com/f1unleashed).

---

*F1 Unleashed is an unofficial project and is not affiliated with, or endorsed by, Formula 1 or
the FIA. F1, FORMULA 1 and related marks are trademarks of Formula One Licensing B.V.; team,
driver, and tyre-supplier marks belong to their respective owners.*
