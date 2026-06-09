# F1Unleashed

![F1Unleashed — race view](screenshots/race.png)

A Formula 1 live-timing and replay application with synchronised audio commentary and per-session deep analysis.

**Release 1.0.0** — May 7, 2026, day of the 2016 Monaco Grand Prix. Celebrating Mclaren's 1000th Grand Prix and 60th anniversary of their first Grand Prix (1966 Monaco Grand Prix).

For what it does and how, see [DOCUMENTATION.md](DOCUMENTATION.md). For a tour of the interface, see [USER_GUIDE.md](USER_GUIDE.md). Known issues are tracked in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

## Built with Claude Code — analysis was human-driven

The implementation (= Python services, processors, JavaScript components, OCR/sync plumbing) was written with [Claude Code](https://claude.com/claude-code). The F1-domain reasoning — what to measure, what and how to classify, what to look out for, how to display the data, 3rd party services to overlay, etc. came from the human in charge. The model is the implementer; the human is the analyst.

---

## Requirements

### Tested environment

- macOS 15+ (= Sequoia / Tahoe). Linux + Windows should work but haven't been actively tested for the live-sync features.
- **Firefox** for the F1Unleashed UI (= reference browser; other modern browsers should work).

### Runtime

- Python 3.13 (= venv recommended).
- `ffmpeg` + `ffprobe` (= audio HLS capture + duration probing for the PDT side-car).
- A formula1.com subscription (= for live sessions; download of historic data may be available without a subscription).

### Audio sync

No external setup required. F1Unleashed captures the commentary HLS feed alongside the data feed and anchors it via the HLS `PROGRAM-DATE-TIME` tag (= continuous re-anchor by a background side-car). The audio displayed in the player is the broadcast UTC of each sample, kept aligned with the data clock without virtual loopbacks or cross-correlation.

### Weather radar

Uses [tomorrow.io](https://www.tomorrow.io/) for radar imagery + forecasts. A free API key is sufficient for normal use.

---

## Installation

```bash
# 1. Clone
git clone <repo-url> f1unleashed && cd f1unleashed

# 2. Python environment
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# edit .env:
#   - NOTIFICATION_WEBHOOK_URL  (= ntfy.sh/<your-topic> for push alerts)
#   - TOMORROW_IO_API_KEY       (= weather radar)

# 4. Start
./service.sh start

# 5. Open
open http://localhost:1950           # 1950 = the year of the first F1 World Championship
```

First-time login:

```bash
python -m app.cli.login          # browser-based F1 login
```

---

## Roadmap

The application covers Practice / Qualifying / Race in usable form today. Active development is focused on these next:


### Coming up soon

- **OCR-based video sync** (= Tesseract over a fixed top-left crop; anchors per session phase: countdown, segment timer, race lights-out, lap counter). Target: Barcelona GP weekend (2026-06-14).
- **Session summary / highlights** (= post-session recap: fastest lap, longest stint, biggest gap closes, position changes, podium).
- **Team radio audio replay** (= per-driver team-radio capture + playback aligned to the data clock).
- **Lift-and-coast** detection.
- **Tyre-saving** detection.
- **Pit windows** (SC / VSC opportunity detection).
- **Pit-strategy** predictions and simulations.
- **Dry/wet** tyre crossover identification.


### Coming up later

- Dockerised deploy.
- Robustness + reconnect logic for long sessions.
- Memory management improvements.

---

## Credits

This project would not exist without the work others have done in this space:

- **[FastF1](https://github.com/theOehrly/Fast-F1)** — Python toolkit for F1 data. Used for session schedule, event metadata, and as the canonical reference for many timing semantics.
- **[Undercut-f1](https://github.com/JustAman62/undercut-f1)** — open-source F1 live-timing analysis app. A constant reference for processor design and live-feed interpretation.
- **[MultiViewer API](https://api.multiviewer.app/)** — circuit metadata (= corners, marshal sectors, DRS zones, layout SVGs). The track maps are generated from this data.
- **[OpenF1](https://openf1.org/)** — schedule + meeting data with circuit info URLs.
- **[tomorrow.io](https://www.tomorrow.io/)** — weather radar imagery and precipitation forecasts.
- **[Tesseract](https://tesseract-ocr.github.io/)** — OCR engine used for visual sync.
- **[Formula 1](https://www.formula1.com/)** — the underlying timing feed, broadcast audio, and on-track data. F1Unleashed is a viewer + analysis layer, not a redistribution of any of the above.

---

## License

[PolyForm Noncommercial 1.0.0](LICENSE). Use for personal, hobby, educational, or research purposes is welcome. Commercial use is not granted under this license. Attribution required.

Copyright © 2026 Nelson Sousa. Co-authored with Claude Code.
