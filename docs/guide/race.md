# User guide — Race

The Race view is tuned for the race itself: gaps, tyre strategy, pit stops, penalties and the
championship picture. Shared controls are on the [Main window](/help/main) page; this page
covers the race-specific features. (Sprint races use this same view.)

![Race view](/static/images/screenshots/race.png)

---

## Standings

Like the practice/qualifying standings, but race-focused:

- **Gaps** — to the leader **and** to the car ahead (the interval), so you can see battles
  forming.
- **Tyre history** — every stint and compound, to read strategy at a glance.
- **Flags & penalties** — blue flags, penalties both **under investigation** and **imposed**,
  and black-and-white (unsporting-conduct) flags.

<!-- SCREENSHOT (new): standings mid-race with a close interval battle, a driver on a
     different strategy (tyre history), and a penalty indicator. -->

---

## Dashboard view — watch the battles

The telemetry tile's **Dashboard** toggle gives the best seat for the race: a two-driver
panel plus a **zoomed, self-centring mini track-map**.

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

<!-- SCREENSHOT (new): Dashboard mode in the race — two cars under a second apart, the
     mini-map zoomed on the chaser mid-corner. -->

---

## Pit stops

A **Pit stops** tab in the Race control tile lists every in-race stop as it happens:

- the **lap**, **driver** and **tyre fitted**;
- the measured **stationary time** and **total time lost**;
- whether the stop was under **green / SC / VSC**;
- the **position change** across the stop and a **traffic** flag if the car rejoined close
  behind another that has yet to pit.

<!-- SCREENSHOT (new): the Pit stops tab with a few stops, showing stationary time, time
     lost, and a position change. -->

---

## Race control, team radio & championship

The Race control tile has several tabs:

- **RCM** — the live race-control message stream, with team-radio clips interleaved by time.
- **Team Radio** — every captured clip, each with Play / Stop; playing one ducks the
  commentary.
- **Pecking order** — the pre-race predicted team ranking and pace.
- **Championship** — provisional drivers' and constructors' standings, updated live from the
  current race order.

---

## Track map & position reconstruction

The circuit map shows every car's position throughout the race, with yellow-flag sectors and
the weather overlay. The positions are **reconstructed to stay faithful even through GPS and
telemetry outages**, so the map keeps a correct picture of the field when the raw feed drops
out.

---

## Support the project

Enjoying F1 Unleashed? You can
[buy me a coffee](https://buymeacoffee.com/f1unleashed).
