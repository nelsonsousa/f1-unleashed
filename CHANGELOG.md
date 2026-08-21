# Changelog

All notable changes to F1Unleashed are documented here, per release. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

This project ships on two tracks — see `CLAUDE.local.md`'s environment model:
- **`main`** — stable release, tagged `vX.Y.Z`.
- **`next`** — public preview, tagged `vX.Y.Z-bN` (beta), ahead of `main`, not yet
  promoted.

## [Unreleased]

Nothing pending.

---

## [2.1.0-b1] (`next`, preview) — 2026-08-21

### Added
- Wall-clock-driven live playhead: the live playback ceiling now advances from
  elapsed real time when the connection is healthy, instead of waiting for the next
  message to physically confirm the edge — fixes the playhead visibly freezing
  between heartbeats. Live-edge margin raised 5s → 15s to match the real
  heartbeat cadence, and choppy audio on a fresh live connect is fixed as a result.
  (`Orts6BRn`, `cJJUzyAj`)

### Changed
- Pipeline redesign completion (WB-1): internal capture/processing changes —
  scheduled-start parsing shared between the SignalR client and CDN fetcher,
  end-of-session analysis now triggers on `SessionStatus=Ends` instead of lagging
  behind capture-task teardown, dead `display_delay_ms` clock parameter removed.
- Processor-handler exceptions now propagate out of `message_bus.emit` instead of
  being silently swallowed — a broken processor will now surface instead of
  failing invisibly.
- Track flag colouring (SC/VSC/Red/Green) scoped to the track outline only.
- Session-status badge shows the F1 Unleashed logo when inactive.
- CDN topic downloads now gated by the session's own `Index.json`, with an alert
  when a new feed topic appears that isn't being downloaded yet.

### Fixed
- Reverted an accidental lag increase in position/telemetry tile smoothing,
  restoring parity with the rest of the UI (`CKlHX0s6`).
- `start_live`'s "already capturing" refusal is now distinguishable from a
  genuine success in the response, instead of looking identical.
- Bounded tolerance added for late-lap `dp` backward drift at pit entry.
- Auto-select (qualifying dashboard): push-lap focus and time-gated at-risk
  narrowing, reducing premature/incorrect driver highlighting.
- Scrubber pre-session green-flag filter given a grace window around session start.

### Known issues / deliberately held back from this preview
- Circuit-signature generation (new capability, from-scratch implementation) —
  needs more real-session validation before promotion; see Trello `FfkO6ELK`.
- WB-16 config-externalisation series (large batch of constants moved to
  `defaults.json`) — deferred as a block, not yet promoted.
- A dead-code cleanup pass touching `signalr_client.py`/`preprocessor.py` — no
  verification evidence exists for it yet.

---

## [2.0.3] (`main`, stable) — 2026-08-21

### Fixed
- **CDN session downloads were returning HTTP 500** on every request due to an
  unsupported keyword argument reaching `SessionPreProcessor.run()` — downloads
  are restored.
- SignalR reconnect burst: the client wasn't told a session had genuinely ended,
  so a dropped connection after `SessionStatus=Ends` reconnected forever,
  replaying a stale subscription snapshot each time (up to 46 replays / 60 min
  observed).
- Lap-boundary detection could lose an entire lap when a future timing crossing
  was wrongly accepted as the current lap's close (one-sided tolerance check made
  symmetric).
- A post-session resend from F1's feed could overwrite correct final lap times —
  including personal bests — with a stale in-progress value; a second, smaller
  defect could also drop a lap time outright.
- Position freeze wasn't reflected in staleness tracking
  (`msSinceLastKnown` could read ~0 during a multi-minute real freeze) — fixed
  with a motion-gated correction that avoids falsely flagging legitimately
  stationary cars as stale.
- Pit-loss time estimate was silently unavailable for Spa-Francorchamps and
  Budapest (stale hardcoded circuit-length table) — restored, and a stale entry
  now logs a visible warning instead of failing silently.
- Auto-select dashboard (qualifying) occasionally picked an inconsistent driver
  due to non-deterministic set iteration in the selection logic.

### Internal
- Telemetry pairing-yield accuracy fix — the core reconstruction fix for this
  project's own documented incident history (2.0.0→2.0.1 telemetry-pairing
  regression class).
- `DpReckoner` shared dead-reckoning module, integrated across position and
  telemetry processors.
- Test tooling, coverage infrastructure, and processor test-coverage batches.

---

## [2.0.2] — 2026-07-24
- fix(track-map): resolve circuit SVG consistently on client + server

## [2.0.1] "Budapest upgrade" — 2026-07-24
- See git history prior to this file's introduction; not backfilled.
