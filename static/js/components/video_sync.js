/**
 * Video sync (Trello: 3-video-sync) — Phase 1: capture + OCR readout.
 *
 * The TV broadcast is watched on mute alongside the (already-synced) data +
 * commentary. This component screen-shares the TV picture, OCRs its session
 * clock, and compares it to the data clock so the two can be aligned.
 *
 *   1. "Video sync" button → getDisplayMedia picks the TV window/tab/screen.
 *   2. The user drags a box over the on-screen session clock (P/Q countdown).
 *      The crop is stored (as fractions of the frame) so it persists.
 *   3. Each second we crop that region, OCR it with Tesseract.js, and show
 *      TV clock vs data clock + the delta.
 *
 * Phase 2 will use the delta to hold the data clock until the TV catches up
 * (TV behind), or warn the user to pause the TV (TV ahead). This phase only
 * READS and reports — it never touches playback.
 */

(function () {
    // Auto-detected timing regions per session type, [x0,y0,x1,y1] as fractions
    // of the 16:9 VIDEO CONTENT rect (not the captured frame) — the video is
    // always 16:9, but on a 16:10 screen it's letterboxed with top/bottom bars.
    // We detect the content rect at runtime (detectContentRect) and map these
    // fractions into it, so it works whether shared from a TV (no bars) or a
    // MacBook (bars). OCR tries the regions in order; first plausible time wins.
    const REGIONS = {
        practice:   [[0.05, 0.122, 0.20, 0.178], [0.04, 0.194, 0.19, 0.250]],  // session clock, then pre-session countdown
        qualifying: [[0.05, 0.122, 0.20, 0.178], [0.04, 0.194, 0.19, 0.250]],
        race:       [[0.05, 0.139, 0.20, 0.172]],                              // lap counter
    };
    function sessionRegions() {
        const t = ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase();
        return REGIONS[t] || REGIONS.practice;
    }
    function isRace() {
        return ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase() === 'race';
    }

    const OCR_UPSCALE = 3;                          // enlarge crop for legibility
    const LUMA_THRESHOLD = 140;                     // light text → dark on white

    // Sampling strategy: on start we OCR back-to-back ("acquire") to catch the
    // exact frame the TV clock ticks; once found we lock to 1 Hz, sampling just
    // after each expected tick (LOCK_OFFSET_MS later) and assuming the TV keeps
    // ticking in step. An unexpected read or a coarse jump drops back to acquire.
    const LOCK_OFFSET_MS = 150;
    // Re-verify the tick phase about once a minute with a short burst (in case it
    // has drifted). The element returning after a dropout also forces a re-check.
    const RECHECK_INTERVAL_MS = 60000;

    // Within INSYNC_S we treat it as synced (the displayed clock is whole-second,
    // so value granularity is 1 s); beyond that we seek the data+audio straight
    // to the TV's position — backward or forward, no waiting.
    const INSYNC_S = 1;

    // Robustness: the synced element (session clock / lap counter) isn't always
    // on screen — commercials, replays — and OCR can misread. We reject reads
    // that don't track our locked countdown and ASSUME WE STAY IN SYNC until the
    // real element returns (never seek on its absence). We only re-acquire when
    // the off-reads form their OWN steady countdown for REACQUIRE_AFTER samples
    // (a genuine new clock, e.g. a session-phase change). After a seek, let the
    // data clock settle before measuring again so we don't thrash.
    const PLAUSIBLE_TOL_MS = 3000;
    const REACQUIRE_AFTER = 4;
    const SEEK_COOLDOWN_MS = 800;

    const state = {
        active: false,
        stream: null,
        video: null,                                // hidden <video> of the stream
        content: { x0: 0, y0: 0, x1: 1, y1: 1 },     // 16:9 video rect within the frame (letterbox-aware)
        worker: null,
        ocrTimer: null,
        jumpFrom: null,                             // data offset at last forward-seek (live-edge check)
        lastTvMs: null,                             // last ACCEPTED TV read (plausibility filter)
        lastTvAt: 0,
        candMs: null, candAt: 0, candCount: 0,      // candidate new clock during off-reads
        cooldownUntil: 0,                           // suppress seeks until this perf-clock time
        mode: 'acquire',                            // 'acquire' (fast) | 'locked' (1 Hz, phase-locked)
        prevSec: null,                              // last whole-second TV value (tick detection)
        tvTickAt: null,                             // perf time of the last detected TV tick
        tvTickVal: null,                            // whole-second value at that tick
        lastReacqAt: 0,                             // perf time the tick was last freshly pinned
        nullStreak: 0,                              // consecutive reads with no element on screen
        // ── race lap-sync ──
        dataLap: null, dataLapAt: 0, lapIntervalMs: 90000, dataTotalLaps: null,
        tvLap: null, tvCross: null, dataCross: null, alignedLap: null, offLapStreak: 0,
    };

    // base.js declares `messageBus` as a top-level const — a global binding but
    // NOT a property of window — so reference it bare, not via window.
    function bus() {
        return (typeof messageBus !== 'undefined') ? messageBus : null;
    }
    function seekTo(offsetS) {
        const b = bus();
        if (b && typeof b.send === 'function') b.send({ cmd: 'seek', offset: Math.max(0, offsetS) });
    }
    function currentOffset() {
        const b = bus();
        return (b && typeof b.getCurrentOffset === 'function') ? b.getCurrentOffset() : null;
    }

    // ── Element + status helpers ─────────────────────────────────────────
    const $ = (id) => document.getElementById(id);

    // The traffic light is the only indicator: green = in sync, yellow =
    // correcting / can't skip forward yet, red = clock unreadable.
    function setLight(cls) {
        const l = $('videoSyncLight');
        if (l) l.className = 'video-sync-light' + (cls ? ' ' + cls : '');
    }

    // ── Clock parsing ────────────────────────────────────────────────────
    // Accepts "H:MM:SS", "MM:SS", "M:SS.s" → milliseconds. Returns null if no
    // sane time is present (OCR noise, blank, etc.).
    function parseClock(str) {
        if (!str) return null;
        const m = String(str).match(/(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.(\d))?/);
        if (!m) return null;
        const h = m[1] ? parseInt(m[1], 10) : 0;
        const min = parseInt(m[2], 10);
        const sec = parseInt(m[3], 10);
        const tenths = m[4] ? parseInt(m[4], 10) : 0;
        if (min > 59 || sec > 59) return null;
        return (((h * 60 + min) * 60 + sec) * 10 + tenths) * 100;
    }

    // The data-side session clock the TV also shows: the header's "Session
    // Time" remaining (#sessionClock), kept live by header.js.
    function dataClockMs() {
        const el = $('sessionClock');
        return el ? parseClock(el.textContent) : null;
    }

    // ── Toggle / lifecycle ───────────────────────────────────────────────
    async function toggle() {
        if (state.active || state.stream) { stop(); return; }
        try {
            state.stream = await navigator.mediaDevices.getDisplayMedia({
                video: { frameRate: { ideal: 30 } }, audio: false,  // ≥10 fps to time the tick edge
            });
        } catch (e) {
            setLight('');                           // share cancelled / denied
            return;
        }
        state.stream.getVideoTracks()[0].addEventListener('ended', stop);
        state.video = document.createElement('video');
        state.video.muted = true;
        state.video.srcObject = state.stream;
        await state.video.play().catch(() => {});
        setLight('arming');
        beginOcr();                                 // auto-region detection by session type
    }

    function stop() {
        if (state.ocrTimer) { clearTimeout(state.ocrTimer); state.ocrTimer = null; }
        if (state.stream) { state.stream.getTracks().forEach(t => t.stop()); state.stream = null; }
        state.video = null;
        state.active = false;
        state.jumpFrom = null;
        state.lastTvMs = null;
        setLight('');
    }

    // ── OCR loop ─────────────────────────────────────────────────────────
    async function ensureWorker() {
        if (state.worker) return state.worker;
        if (typeof Tesseract === 'undefined') { setLight('error'); return null; }
        const worker = await Tesseract.createWorker('eng');
        await worker.setParameters({
            tessedit_char_whitelist: '0123456789:./',  // clock MM:SS and lap n/total
            tessedit_pageseg_mode: '7',             // single text line
        });
        state.worker = worker;
        return worker;
    }

    // Detect the 16:9 video rect inside the captured frame by trimming near-black
    // letterbox/pillarbox bars, so region fractions map to the video, not the
    // bars. Falls back to the full frame if detection looks degenerate.
    const _dc = document.createElement('canvas');
    function detectContentRect() {
        const v = state.video;
        if (!v || !v.videoWidth) return { x0: 0, y0: 0, x1: 1, y1: 1 };
        const w = 320, h = Math.max(1, Math.round(320 * v.videoHeight / v.videoWidth));
        _dc.width = w; _dc.height = h;
        const ctx = _dc.getContext('2d');
        ctx.drawImage(v, 0, 0, w, h);
        const d = ctx.getImageData(0, 0, w, h).data;
        const rowDark = (y) => { for (let x = 0; x < w; x++) { const i = (y * w + x) * 4; if (d[i] > 24 || d[i + 1] > 24 || d[i + 2] > 24) return false; } return true; };
        const colDark = (x) => { for (let y = 0; y < h; y++) { const i = (y * w + x) * 4; if (d[i] > 24 || d[i + 1] > 24 || d[i + 2] > 24) return false; } return true; };
        let top = 0; while (top < h && rowDark(top)) top++;
        let bot = h - 1; while (bot > top && rowDark(bot)) bot--;
        let left = 0; while (left < w && colDark(left)) left++;
        let right = w - 1; while (right > left && colDark(right)) right--;
        if (bot - top < h * 0.5 || right - left < w * 0.5) return { x0: 0, y0: 0, x1: 1, y1: 1 };
        return { x0: left / w, y0: top / h, x1: (right + 1) / w, y1: (bot + 1) / h };
    }

    const _c = document.createElement('canvas');
    function cropToCanvas(region) {
        const v = state.video;
        if (!region || !v) return null;
        const c = state.content;                    // content-relative → frame fractions
        const cw = c.x1 - c.x0, ch = c.y1 - c.y0;
        const fx0 = c.x0 + region[0] * cw, fy0 = c.y0 + region[1] * ch;
        const fx1 = c.x0 + region[2] * cw, fy1 = c.y0 + region[3] * ch;
        const sx = fx0 * v.videoWidth, sy = fy0 * v.videoHeight;
        const sw = (fx1 - fx0) * v.videoWidth, sh = (fy1 - fy0) * v.videoHeight;
        if (sw < 1 || sh < 1) return null;
        _c.width = Math.round(sw * OCR_UPSCALE);
        _c.height = Math.round(sh * OCR_UPSCALE);
        const ctx = _c.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(v, sx, sy, sw, sh, 0, 0, _c.width, _c.height);
        // Binarise: light text → black on white (Tesseract prefers dark-on-light).
        const img = ctx.getImageData(0, 0, _c.width, _c.height);
        const d = img.data;
        for (let i = 0; i < d.length; i += 4) {
            const luma = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
            const v2 = luma > LUMA_THRESHOLD ? 0 : 255;
            d[i] = d[i + 1] = d[i + 2] = v2; d[i + 3] = 255;
        }
        ctx.putImageData(img, 0, 0);
        return _c;
    }

    // Plausibility filter: a real clock counts down ~1 s per real second, so a
    // good read tracks the last one. Reject OCR misreads (which would seek the
    // data wildly); re-acquire if it stays off (a genuine jump). Returns the
    // accepted TV ms, or null when this read can't be trusted.
    function acceptTv(tvMs, now) {
        if (tvMs == null) return null;                  // nothing on screen → caller assumes sync
        if (state.lastTvMs != null) {
            const expected = state.lastTvMs - (now - state.lastTvAt);
            if (Math.abs(tvMs - expected) > PLAUSIBLE_TOL_MS) {
                // Off our locked countdown (commercial overlay / replay clock / misread).
                // Adopt a NEW reference only if these off-reads form their own steady
                // countdown for a while; otherwise assume we're still in sync.
                const candExp = state.candMs != null ? state.candMs - (now - state.candAt) : null;
                state.candCount = (candExp != null && Math.abs(tvMs - candExp) <= PLAUSIBLE_TOL_MS)
                    ? state.candCount + 1 : 1;
                state.candMs = tvMs; state.candAt = now;
                if (state.candCount >= REACQUIRE_AFTER) {   // a genuine new clock → re-acquire
                    state.lastTvMs = tvMs; state.lastTvAt = now;
                    state.candMs = null; state.candCount = 0;
                    return tvMs;
                }
                return null;                                 // treat as no-read for now
            }
        }
        state.candMs = null; state.candCount = 0;
        state.lastTvMs = tvMs; state.lastTvAt = now;
        return tvMs;
    }

    // Enter the fast capture phase to (re-)pin the tick instant. Keeps tvTickAt
    // as a fallback — the TV's tick is unaffected by our data seeks.
    function toAcquire() {
        state.mode = 'acquire'; state.prevSec = null;
        state.candMs = null; state.candCount = 0;
    }

    async function beginOcr() {
        state.active = true;
        setLight('adjust');
        const worker = await ensureWorker();
        if (!worker) { stop(); return; }
        state.content = detectContentRect();            // strip letterbox bars (16:9 video rect)
        if (isRace()) { resetRace(); runRaceLoop(); }   // lap-increment sync
        else { toAcquire(); runLoop(); }                // P/Q clock tick sync
    }

    async function runLoop() {
        if (!state.active) return;
        // Hidden tab/window → timers throttle and the capture freezes, so reads
        // are stale. Idle until visible, then re-acquire the tick phase cleanly.
        if (document.hidden) { toAcquire(); state.ocrTimer = setTimeout(runLoop, 1000); return; }

        const now = performance.now();
        // OCR the session's candidate regions in priority order; first that yields
        // a plausible time wins (e.g. ongoing clock, else the pre-session countdown).
        let raw = null;
        for (const region of sessionRegions()) {
            const canvas = cropToCanvas(region);
            if (!canvas) continue;
            try {
                const { data } = await state.worker.recognize(canvas);
                const t = parseClock(data.text);
                if (t != null) { raw = t; break; }
            } catch (e) { /* transient OCR error */ }
            if (!state.active) return;
        }
        if (!state.active) return;

        const tvMs = acceptTv(raw, now);
        if (tvMs == null) state.nullStreak++; else state.nullStreak = 0;
        const dMs = dataClockMs();
        const inSync = (tvMs != null && dMs != null) && Math.abs(tvMs - dMs) <= INSYNC_S * 1000;

        correct(tvMs, dMs, now, inSync);

        // Tick detection: a clean 1-second decrement is a real tick. Use it to
        // lock the phase (once coarse-aligned). An unexpected value — including
        // the element returning after a dropout (it won't be prevSec−1) — drops
        // back to acquire so we re-check the tick instant.
        if (tvMs != null) {
            const sec = Math.round(tvMs / 1000);
            if (state.prevSec != null && sec === state.prevSec - 1) {
                state.tvTickAt = now; state.tvTickVal = sec;
                if (state.mode === 'acquire' && inSync) { state.mode = 'locked'; state.lastReacqAt = now; }
            } else if (state.mode === 'locked' && state.prevSec != null && sec !== state.prevSec) {
                toAcquire();
            }
            state.prevSec = sec;
        }

        // Schedule the next sample.
        let delay;
        if (state.mode === 'locked' && state.tvTickAt != null) {
            if (now - state.lastReacqAt > RECHECK_INTERVAL_MS) {
                toAcquire();                         // periodic short burst to re-pin the tick
                delay = 0;
            } else {
                const k = Math.floor((performance.now() - state.tvTickAt) / 1000);
                delay = Math.max(20, state.tvTickAt + (k + 1) * 1000 + LOCK_OFFSET_MS - performance.now());
            }
        } else {
            // acquire: back-to-back while the element is on screen (catch the tick
            // frame); throttle to 1 Hz if it's gone (commercial/replay) so we don't
            // spin — we still assume sync (green) meanwhile.
            delay = state.nullStreak >= 3 ? 1000 : 0;
        }
        state.ocrTimer = setTimeout(runLoop, delay);
    }

    // Seek the data+audio straight to the TV's clock position — either direction,
    // no waiting. Both clocks count down, so TV behind ⇒ tvMs > dMs ⇒ data ahead
    // ⇒ seek back; TV ahead ⇒ seek forward. After any jump, re-acquire the phase.
    //   green = in sync   yellow = correcting / can't skip forward yet   red = no read
    function correct(tvMs, dMs, now, inSync) {
        if (tvMs == null || dMs == null) {
            // Synced element not on screen (commercial / replay) or unreadable —
            // assume we're still in sync and wait; only show "searching" (yellow)
            // before we've ever locked on.
            setLight(state.lastTvMs != null ? 'ok' : 'adjust');
            return;
        }
        if (inSync) { state.jumpFrom = null; setLight('ok'); return; }
        if (now < state.cooldownUntil) { setLight('adjust'); return; }   // let a prior seek settle

        const cur = currentOffset();
        if (cur == null) { setLight('error'); return; }
        const deltaS = (tvMs - dMs) / 1000;
        if (deltaS < 0) {
            // TV ahead → seek forward. If the last attempt didn't advance, there's
            // no data ahead yet (live edge) → stay yellow and wait, don't spam.
            if (state.jumpFrom != null && (cur - state.jumpFrom) < 0.5) { setLight('adjust'); return; }
            state.jumpFrom = cur;
        } else {
            state.jumpFrom = null;                 // backward is always reachable
        }
        seekTo(cur + (dMs - tvMs) / 1000);
        state.cooldownUntil = now + SEEK_COOLDOWN_MS;
        toAcquire();                               // data jumped → re-lock the phase
        setLight('adjust');
    }

    // Becoming visible again: re-acquire the tick phase from scratch.
    document.addEventListener('visibilitychange', () => {
        if (state.active && !document.hidden) toAcquire();
    });

    // ── Race lap-sync ────────────────────────────────────────────────────
    // The data leader lap arrives on the `raceLaps` topic; the TV lap badge is
    // OCR'd. At 1 Hz we confirm the same lap; near the lap end (derived ≥95% of
    // the running lap time) we burst to catch the TV's exact lap-increment frame
    // and align it to the data's lap-cross. Losing sync mid-lap is acceptable.

    function resetRace() {
        state.tvLap = null; state.tvCross = null; state.alignedLap = null;
        state.offLapStreak = 0; state.cooldownUntil = 0;
        // dataLap / dataLapAt / lapIntervalMs are maintained by the raceLaps sub.
    }

    function parseLap(str) {
        if (!str) return null;
        let m = String(str).match(/(\d{1,2})\s*\/\s*(\d{1,3})/);   // "6/66" → leader 6
        if (m) return parseInt(m[1], 10);
        m = String(str).match(/\b(\d{1,2})\b/);                    // fallback: first 1–2 digits
        return m ? parseInt(m[1], 10) : null;
    }

    // Accept a TV lap read only if it tracks the count-up (== last or last+1), or
    // matches the data lap (re-acquire). Filters OCR misreads / merged digits.
    function acceptLap(lap) {
        if (lap == null) return null;
        if (state.tvLap != null && lap !== state.tvLap && lap !== state.tvLap + 1) {
            return lap === state.dataLap ? lap : null;
        }
        return lap;
    }

    // Pair the data and TV lap-cross for the same lap and seek so they coincide.
    function tryAlign(now) {
        const d = state.dataCross, t = state.tvCross;
        if (!d || !t || d.lap !== t.lap || state.alignedLap === d.lap) return;
        if (now < state.cooldownUntil) return;
        const cur = currentOffset();
        if (cur == null) return;
        state.alignedLap = d.lap;
        const offsetS = (d.at - t.at) / 1000;   // +: data crossed later ⇒ behind ⇒ seek forward
        if (Math.abs(offsetS) < 0.15) { setLight('ok'); return; }
        seekTo(cur + offsetS);
        state.cooldownUntil = now + SEEK_COOLDOWN_MS;
        setLight('adjust');
    }

    async function runRaceLoop() {
        if (!state.active) return;
        if (document.hidden) { state.ocrTimer = setTimeout(runRaceLoop, 1000); return; }

        const now = performance.now();
        let lap = null;
        const canvas = cropToCanvas(sessionRegions()[0]);   // lap-counter region
        if (canvas) {
            try { const { data } = await state.worker.recognize(canvas); lap = parseLap(data.text); }
            catch (e) { /* transient OCR error */ }
        }
        if (!state.active) return;
        lap = acceptLap(lap);

        // TV lap-cross detection (count-up by 1).
        if (lap != null && state.tvLap != null && lap === state.tvLap + 1) {
            state.tvCross = { lap, at: now };
            tryAlign(now);
        }
        if (lap != null) state.tvLap = lap;

        // Coarse / light: same lap as the data?
        if (lap == null || state.dataLap == null) {
            setLight(state.tvLap != null ? 'ok' : 'adjust');   // badge gone → assume in sync
        } else if (lap === state.dataLap) {
            state.offLapStreak = 0; setLight('ok');
        } else {
            // Sustained whole-lap mismatch (not a near-cross ±1): best-effort coarse
            // seek by the lap difference × the running lap time.
            state.offLapStreak++;
            const frac = state.dataLapAt ? (now - state.dataLapAt) / state.lapIntervalMs : 0;
            if (state.offLapStreak >= 3 && frac < 0.9 && now >= state.cooldownUntil) {
                const cur = currentOffset();
                if (cur != null) {
                    seekTo(cur - (state.dataLap - lap) * state.lapIntervalMs / 1000);
                    state.cooldownUntil = now + SEEK_COOLDOWN_MS;
                    state.offLapStreak = 0;
                }
            }
            setLight('adjust');
        }

        // Burst near the expected lap-cross (≥95% of the lap), else 1 Hz.
        let burst = false;
        if (state.dataLapAt && state.dataLap === state.tvLap) {
            const frac = (now - state.dataLapAt) / state.lapIntervalMs;
            burst = frac >= 0.95 && frac < 1.3;
        }
        state.ocrTimer = setTimeout(runRaceLoop, burst ? 120 : 1000);
    }

    // Data leader lap (+ derive the running lap time from cross intervals). This
    // runs whenever raceLaps fires; alignment only while video sync is active.
    messageBus.on('raceLaps', (data) => {
        if (!data || typeof data.currentLap !== 'number') return;
        if (data.totalLaps) state.dataTotalLaps = data.totalLaps;
        const L = data.currentLap;
        if (state.dataLap != null && L !== state.dataLap) {
            const now = performance.now();
            if (state.dataLapAt) {
                const iv = now - state.dataLapAt;
                if (iv > 20000 && iv < 300000)              // sane lap time → smooth
                    state.lapIntervalMs = 0.5 * state.lapIntervalMs + 0.5 * iv;
            }
            state.dataLapAt = now;
            state.dataCross = { lap: L, at: now };
            if (state.active && isRace()) tryAlign(now);
        }
        state.dataLap = L;
    });

    // ── Race-start anchor (ENTER) ────────────────────────────────────────
    // Press ENTER at lights-out on the broadcast to jump the data+audio to the
    // race start (the GREEN flag = lights-out, known upfront from session:events).
    // Works whether or not a pre-race countdown is visible — the countdown is
    // just the user's visual cue for when to press. The race lap-increment sync
    // then keeps everything aligned from lap 1 onward.
    let raceStartMs = null;
    messageBus.on('session:events', (events) => {
        if (!Array.isArray(events)) return;
        const greens = events
            .filter(e => String((e.data && (e.data.event || e.data)) || '').toUpperCase() === 'GREEN')
            .map(e => e.offset_ms)
            .filter(o => typeof o === 'number');
        if (greens.length) raceStartMs = Math.min(...greens);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.isComposing) return;
        if (!isRace() || raceStartMs == null) return;
        e.preventDefault();
        if (typeof seekToOffset === 'function') seekToOffset(raceStartMs / 1000);
    });

    window.toggleVideoSync = toggle;
})();
