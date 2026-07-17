# User guide — Race

The Race view is tuned for the race itself: gaps, tyre strategy, pit stops, penalties and the
championship picture. Shared controls are on the [Main window](/help/main) page; this page
covers the race-specific features. (Sprint races use this same view.)

<p align="center"><img src="/static/images/screenshots/race.png" width="1200"></p>


---

## Standings

Like the practice/qualifying standings, but race-focused:

- **Gaps** — to the leader **and** to the car ahead (the interval), so you can see battles
  forming.
- **Tyre history** — every stint and compound, to read strategy at a glance.
- **Flags & penalties** — blue flags, penalties both **under investigation** and **imposed**,
  and black-and-white (unsporting-conduct) flags.

<p align="center"><img src="/static/images/screenshots/race_standings.png" width="800"></p>


---

## Dashboard view — watch the battles

The telemetry tile opens in the **Dashboard** view — the best seat for the race: a two-driver
panel plus a **zoomed, self-centring mini track-map**. (Toggle to **Telemetry** for the trace
chart.)

- Each side shows the driver's **TLA**, **position**, the **interval** between the two cars,
  and status — a **pit** indicator, tyre compound and age, and a close-battle highlight when
  the gap is under a second.
- The **mini-map** follows the chasing car, zoomed in, so you can see the two cars converge
  through the corners.
- **Auto-select** (on by default) picks the **best battle on track**: the frontmost pair
  within striking distance (a strong move when the interval is under half a second, a softer
  one under a second), falling back to the leaders. It skips cars that have finished. When a
  pass or a lap settles, the pair is held briefly before switching.
- Click a **TLA** to override; the **Auto-select** button toggles it back on.

<p align="center"><img src="/static/images/screenshots/race_dashboard.png" width="800"></p>

---

## Pit stops

A **Pit stops** tab in the Race control tile lists every in-race stop as it happens:

- the **lap**, **driver** and **tyre fitted**;
- the measured **stationary time** and **total time lost**;
- whether the stop was under **green / SC / VSC**;
- the **position change** across the stop and a **traffic** flag if the car rejoined close
  behind another that has yet to pit.

<p align="center"><img src="/static/images/screenshots/pitstops.png" width="400"></p>

---

## Race control, team radio & championship

The Race control tile has several tabs:

- **Race control** — the live race-control message stream, with team-radio clips interleaved by time.
- **Team Radio** — every captured clip, each with Play / Stop; playing one ducks the
  commentary.
- **Championship** — provisional drivers' and constructors' standings, updated live from the
  current race order.

<p align="center"><img src="/static/images/screenshots/championship.png" width="400"></p>


---

## Track map

The circuit map shows every car's position throughout the race, with yellow-flag sectors and
the weather overlay.

Car positions come from **live timing's GPS position data**. That feed has known issues: it
sometimes produces unreliable information — **especially around the pit lane** — and suffers
occasional **data outages**. When GPS data is missing or unreliable, the app does its best to
**reconstruct the position from the telemetry data**, then tries to match the calculated speed
trace to a known circuit signature. This algorithm is **still in its early stages and lacks
reliability** — the car is always placed on the map, but the positions will suffer from
**frequent corrections**.

<p align="center"><img src="/static/images/screenshots/track_map_outage.png" width="500"></p>



<p align="center"><img src="/static/images/screenshots/checkered_flag.png" width="45"></p>