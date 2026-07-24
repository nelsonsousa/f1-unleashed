# User guide — Practice

The Practice view is tuned for **free practice**: lots of timed-lap context, pace
classification, tyre history and telemetry comparison. Everything shared with the other
views — the player, audio, weather, TV sync, status bar — is covered on the
[Main window](/help/main) page. This page covers what's specific to practice.

<p align="center"><img src="/static/images/screenshots/practice.png" width="1200"></p>

---

## Standings

The standings tile is the heart of the practice view. Per driver it shows:

- **Position** and **driver**;
- **Lap type** — each lap is classified (PUSH / SLOW / OUT / PIT / STOP / CHECKERED), derived
  from telemetry and lap-time deltas;
- **Best lap** and **gap to leader**;
- **Mini-sectors** and **S1 / S2 / S3** times, colour-coded (purple = session best, green =
  personal best, yellow = slower);
- **Last lap** time;
- **Tyre history** — the stints and compounds run so far;
- **Lap count**.

<p align="center"><img src="/static/images/screenshots/practice_standings.png" width="800"></p>

---

## Telemetry tile

The telemetry tile has two views, switched with the **Dashboard / Telemetry** toggle in its
title bar. It **opens in the Dashboard view**.

### Dashboard view (default)

A focused **two-driver** view: live gauges plus a mini telemetry viewer per driver.

- **Auto-select** (on by default) picks the two most interesting drivers for you. In
  practice that's the drivers **closest to finishing a push lap** — so you catch fast laps as
  they're set. When nobody is pushing, it holds.
- Click a driver's **TLA** to take over manually; that turns auto-select off. Toggle it back
  on with the **Auto-select** button.

<p align="center"><img src="/static/images/screenshots/practice_dashboard.png" width="800"></p>

### Telemetry view

Toggle to **Telemetry** for the trace chart: **speed / RPM / gear / throttle+brake** traces (throttle and brake share one channel)
with a per-driver lap list, so you can compare laps and drivers.

- Show the **live** trace, the driver's **last** lap, their **best** lap, or a **selection**
  of laps overlaid for comparison.
- **Driver pills** and **lap pills** carry each driver's colour and TLA; selecting a lap
  selects its driver automatically.
- **Corner labels** along the x-axis line up with the circuit map, so you can read where on
  the lap a gain or loss happens.

<p align="center"><img src="/static/images/screenshots/telemetry.png" width="800"></p>

---

## Track map

The circuit map shows every car's live position, with yellow-flag sectors highlighted and
the weather/rain-radar overlay (see [Weather](/help/main#weather)).

<p align="center"><img src="/static/images/screenshots/track_map.png" width="500"></p>

Car positions come from **live timing's GPS position data**, which has known issues — it can be
unreliable, **especially around the pit lane**, and suffers occasional **outages**. When GPS
data is missing or unreliable, the app does its best to **reconstruct the position from the
telemetry** and match the calculated speed trace to a known circuit signature. This algorithm is
**still in its early stages and lacks reliability**, so positions are always shown but will
suffer from **frequent corrections**.

---

## Race control & team radio

The Race control tile carries the **RCM** message stream (race-control messages with
team-radio clips interleaved by time) and a **Team Radio** tab listing every captured clip.
Each clip has Play / Stop; playing one ducks the commentary for its duration.


<p align="center"><img src="/static/images/icons/logo_light.svg" width="120"></p>