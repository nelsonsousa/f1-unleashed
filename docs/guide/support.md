# User guide — Support

Release history, answers to the questions that come up most often, and where to report a
bug or ask for a feature. Shared controls are on the [Main window](/help/main) page; the
session pages cover Practice, Qualifying and Race.

---

## Release history

**2.0.0 — "Spa-Francorchamps" (16 July 2026).** The **Live Dashboard** — a focused
two-driver view on the telemetry tile: live gauges with a **lap-time forecast** in
practice/qualifying, and a **battle panel** with a self-centring mini track-map in the race.
Adds **auto-select** (the app picks the two most interesting drivers and re-picks as the
session evolves), a **pecking-order predictor**, **pit-stop measurement**, and this split
user guide plus a **Player help** pop-up.

**1.3.** **Automatic audio sync** — the commentary anchors itself to the broadcast, so it
aligns to the data clock with no manual step. Unified live/replay audio, robust live-edge
audio, and race-start anchoring (jump straight to lights-out).

**1.2.** **In-app settings** (a settings dialog backed by a JSON store, replacing the old
`.env`), **team-radio replay** (clips captured live and played back time-aligned), the
**status footer + data-health monitor**, and the **weather-forecast** widget.

**1.0.0 — first release (7 June 2026).** Shipped on the day of the 2026 Monaco Grand Prix —
McLaren's 1000th Grand Prix start, 60 years after their first race, which was also at Monaco.

---

## Frequently asked questions

### Installation & first run

**What do I need to run it?** Python 3.13, `ffmpeg` (with `ffprobe`) on your `PATH`, and a
**formula1.com** subscription for live sessions and full data. macOS is the tested platform;
Linux and Windows should work but aren't actively tested for the live-sync features. Firefox
is the reference browser.

**Is there a config file to edit?** No. Everything has a default, so the app runs straight
after `./f1unleashed.sh start` (it serves on **port 1950**). Settings live in the in-app
dialog — the gear on the home-page footer — not in a `.env`.

**The page won't load.** Make sure the server started (`./f1unleashed.sh status`) and that
nothing else is using port 1950, then open `http://localhost:1950`.

### Logging in

**Why do I have to log in again every few days?** The F1 login token lasts about **72 hours**.
The app warns you before it expires (the lead time is a setting), then you log in again from
the home page (**Login**) or with `python -m app.cli.login`.

**Login only works in a browser window — why?** F1's auth has bot protection, so a headless
login is rejected. The app always opens a real browser window to complete the login; the token
it captures is then reused until it expires.

### Downloading sessions & the F1 CDN

**Can I download a past session?** Yes. Open the event on the home page, pick a session and
click **Download** — the raw timing and telemetry data is pulled from F1's live-timing CDN and
replays fully, seekably, offline.

**What can't be downloaded after the fact?** The **broadcast commentary audio**. F1 archives
the timing data on its CDN, but the commentary is a live stream it does **not** keep — so it
can only be recorded **while the session is live**. If the app wasn't running during the
session, a later download gives you the full data and telemetry, but **no commentary audio**.
Live capture happens automatically when a session goes live, so the reliable way to have
commentary for a replay is to let the app capture the session as it airs.

**Downloads fail on a VPN.** F1's CDN blocks requests from many VPN and data-centre IP ranges.
Download on a normal residential connection.

### Audio — commentary & team radio

**The commentary lags the on-track action.** A fixed delay is inherent to the broadcast
commentary feed itself, and the app anchors around it automatically for both live and replay,
across seeks and speed changes. If you ever want to nudge it, the **Delay** box next to the
volume control (`ss.SSS`, ±) is a manual fallback — positive plays the commentary later,
negative earlier.

**A downloaded session has no commentary.** Commentary is captured live only (see above); a
later download won't have it. Team radio and the timing data are unaffected.

### The live feed & disconnections

**The message rate drops to zero mid-session — is it broken?** Usually not. F1's feed only
sends data when cars are **on track**, so it goes quiet between runs (red flags, long
stoppages, the gap before a session starts). The clock keeps running; data resumes when the
cars do.

**The connection dropped.** The app reconnects on its own and catches up on the buffered
messages, so you don't lose the session. Live capture also has a no-data timeout so a genuinely
dead feed doesn't hang the capture.

### Cache & storage

**Where are downloaded sessions stored?** On disk, in a per-session cache. The location is
shown and configurable in **Settings → Cache location**; it defaults to an OS-appropriate
app-data folder.

**Can I move the cache to another drive?** Yes — change the cache location in Settings. It
offers to move your existing cache to the new folder, and the change **requires a restart**.

**How do I free up space?** Delete a session from its card on the home page — that removes it
from disk. You can also turn off "keep downloaded files" per session type in Settings.

---

## Found a bug? Have an idea?

F1 Unleashed is developed in the open. To **report a bug**, leave a comment, or **suggest a
feature**, open an issue on the project's GitHub:

**[github.com/nelsonsousa/f1-unleashed/issues](https://github.com/nelsonsousa/f1-unleashed/issues)**

The more detail the better — what you did, what you expected, the session and moment it
happened, and anything from the status bar (mode, message rate, the TIMING / TELEMETRY /
POSITION health boxes) that looked off.

---

## Support the project

F1 Unleashed is a free, personal project built to make watching Formula 1 better. If it
improves your race weekends, you can support it: [buy me a coffee](https://buymeacoffee.com/f1unleashed).

<p align="center"><img src="/static/images/screenshots/checkered_flag.png" width="45"></p>
