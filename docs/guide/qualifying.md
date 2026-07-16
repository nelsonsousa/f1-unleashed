# User guide — Qualifying

The Qualifying view builds on the practice layout and adds the things that matter under
knockout pressure: the **elimination zone**, a **live lap-time forecast**, and a **predicted
pecking order**. Shared controls are on the [Main window](/help/main) page; this page covers
the qualifying-specific features. (Sprint Qualifying uses this same view.)

![Qualifying view](/static/images/screenshots/qualifying.png)

---

## Standings under knockout pressure

- **Elimination zone** — drivers in the drop zone are marked, and their **gap is shown to the
  driver on the bubble** (the last car currently safe), so you can see exactly who needs to
  find time.
- **Current tyre only** — qualifying shows the tyre on the car now, not full stint history.
- **Live delta on a flying lap** — while a driver is on a qualifying attempt, their **delta to
  their own best lap** is shown live, along with the **positions it would gain or lose**. When
  the lap completes, the standings switch to the **actual** delta and the positions actually
  gained.

<!-- SCREENSHOT (new): standings mid-Q1 with the drop zone highlighted, a bubble gap, and a
     driver mid-lap showing a live delta + projected positions. -->

---

## Lap-time forecast

During a flying lap the app **forecasts the lap time** from the telemetry as the driver
progresses, rather than waiting for the lap to complete.

- In the Dashboard view (the telemetry tile's default) the driver's stopwatch is labelled
  **FORECAST** while the lap is running and shows the projected time; it switches to **LAP
  TIME** once the lap is confirmed.
- The forecast is what drives the live "positions gained" projection in the standings.

---

## Dashboard view & auto-select

As in practice, the telemetry tile opens in the **Dashboard** view (two-driver gauges + mini
telemetry viewers; toggle to **Telemetry** for the trace chart). **Auto-select** is tuned for
qualifying:

- **Q1 / Q2** — it prioritises the **at-risk** drivers on a push lap: those in the drop zone
  plus the four places just above the cutoff, ordered by who is closest to finishing their
  lap. These are the laps that decide who goes through.
- **Q3** — it follows the **fight for pole**: the predicted and current **top five**, ordered
  by track position.

When a driver completes a lap, the pair is held for a few seconds so you can read the time
and result before it switches to the next drivers. Click a **TLA** to override; the
**Auto-select** button toggles it back on.

<!-- SCREENSHOT (new): Dashboard mode in Q1 showing two at-risk drivers on push laps, one
     with a FORECAST stopwatch. -->

---

## Pecking order

A **Pecking order** tab in the Race control tile shows the **predicted ranking of the teams**
and their gaps — an at-a-glance read of the competitive order as the session refines it.

<!-- SCREENSHOT (new): the Pecking order tab with the predicted team ranking and gaps. -->

---

## Telemetry (trace) view

Toggled from the Dashboard, the trace view works as in practice, with one addition: a control
**groups the lap list by part (Q1 / Q2 / Q3)** so you can compare a driver's laps within and
across segments. Corner labels line up with the circuit map.

---

## Support the project

Enjoying F1 Unleashed? You can
[buy me a coffee](https://buymeacoffee.com/f1unleashed).
