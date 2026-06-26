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
    // Pre-race countdown — LAYOUT-AGNOSTIC. The countdown graphic's size/position
    // varies by broadcaster and even by graphic (a small persistent badge vs a
    // large "STARTS IN" overlay), so we can't hardcode a tight box. We OCR a WIDE
    // upper-left sweep, regex out every MM:SS, and accept the one that actually
    // ticks DOWN at wall-clock rate (the real countdown; static numbers don't).
    const RACE_COUNTDOWN_REGION = [0.0, 0.04, 0.22, 0.30];  // wide upper-left sweep
    const CD_MAX_MS = 45 * 60 * 1000;   // sane countdown ceiling
    const CD_MATCH_TOL_MS = 1200;       // |observed − (anchor − elapsed)| to be "the same countdown"
    const CD_CONFIRM_MS = 2500;         // must track this long (drop ~2.5s) before we trust it

    // P/Q session clock — same robustness as the race countdown. The timer's exact
    // position varies by broadcast/layout, and a tight box lands on the wrong row
    // (a leaderboard line, not the clock). So we OCR a WIDE upper-left sweep over the
    // whole timing graphic, regex every H:MM:SS / MM:SS, and pick the candidate
    // nearest the data session clock (leaderboard gaps / driver numbers aren't times;
    // lap times fall outside the nearness window). "OCR more and hit, than less and miss."
    const PQ_CLOCK_SWEEP = [0.0, 0.0, 0.35, 0.45];  // wide upper-left block (fractions of the 16:9 content rect)
    const PQ_MATCH_TOL_MS = 90000;                  // accept the MM:SS candidate within 90s of the data clock

    const OCR_UPSCALE = 3;                          // enlarge crop for legibility
    const LUMA_THRESHOLD = 140;                     // light text → dark on white
    const WHITE_THR = 165;                          // near-white caption isolation (wide sweep)

    // Sampling strategy: on start we OCR back-to-back ("acquire") to catch the
    // exact frame the TV clock ticks; once found we lock to 1 Hz, sampling just
    // after each expected tick (LOCK_OFFSET_MS later) and assuming the TV keeps
    // ticking in step. An unexpected read or a coarse jump drops back to acquire.
    const LOCK_OFFSET_MS = 150;
    // Re-verify the tick phase about once a minute with a short burst (in case it
    // has drifted). The element returning after a dropout also forces a re-check.
    const RECHECK_INTERVAL_MS = 60000;

    // Within INSYNC_S we treat it as synced and DON'T re-sync; beyond that we seek
    // the data+audio straight to the TV's position — backward or forward.
    const INSYNC_S = 0.5;
    // The server takes a moment to fetch the exact instant, so seeks land a touch
    // short. Bias ENTER / "+" jumps this far AHEAD of the requested instant
    // (manual "−" pause/play and the scrubber are unaffected).
    const LAG_COMP_S = 0.5;

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
    // De-sync is gradual, so sample sparsely to keep a laggy browser responsive (the
    // wide OCR crop is the heavy part). Prefer pausing over seeking; when a forward
    // seek IS needed, overshoot so we land ahead and then pause-sync back, rather
    // than chasing the TV with repeated seeks.
    const OCR_INTERVAL_MS = 5000;   // P/Q clock sample cadence
    const FWD_OVERSHOOT_S = 5;      // a forward seek lands this far AHEAD of the TV
    const MAX_PAUSE_S = 20;         // cap a single pause-to-resync hold

    const state = {
        active: false,
        stream: null,
        video: null,                                // hidden <video> of the stream
        content: { x0: 0, y0: 0, x1: 1, y1: 1 },     // 16:9 video rect within the frame (letterbox-aware)
        worker: null,
        sparseWorker: null,                         // psm-11 worker for the wide countdown sweep
        textWorker: null,                           // psm-6 alphanumeric worker for the P/Q header line
        cdAnchors: [],                              // [{val,at}] countdown candidates being verified
        ocrTimer: null,
        jumpFrom: null,                             // data offset at last forward-seek (live-edge check)
        lastTvMs: null,                             // last ACCEPTED TV read (plausibility filter)
        lastTvAt: 0,
        candMs: null, candAt: 0, candCount: 0,      // candidate new clock during off-reads
        cooldownUntil: 0,                           // suppress seeks until this perf-clock time
        pausedBySync: false,                        // we paused playback to let the TV catch up
        resumeTimer: null,                          // pending resume after a sync-pause
        maxOffset: null,                            // live edge / total available offset (s)
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
    // Pause playback so the TV can catch up; resume picks up where it froze (no seek).
    function pauseData() {
        const b = bus();
        if (b && b.isPlaying && typeof b.send === 'function') { b.send({ cmd: 'pause' }); state.pausedBySync = true; }
    }
    function resumeData() {
        if (state.resumeTimer) { clearTimeout(state.resumeTimer); state.resumeTimer = null; }
        const b = bus();
        if (state.pausedBySync && b && typeof b.send === 'function') b.send({ cmd: 'play' });
        state.pausedBySync = false;
    }
    function seekLiveCmd() {
        const b = bus();
        if (b && typeof b.send === 'function') b.send({ cmd: 'seek_live' });
    }
    // The live edge / total available offset (seconds) — the furthest we can seek to.
    messageBus.on('state:clock', (d) => { if (d && typeof d.duration === 'number') state.maxOffset = d.duration; });

    // ── Element + status helpers ─────────────────────────────────────────
    const $ = (id) => document.getElementById(id);

    // The traffic light is the only indicator: green = in sync, yellow =
    // correcting / can't skip forward yet, red = clock unreadable.
    function setLight(cls) {
        const l = $('videoSyncLight');
        if (l) l.className = 'video-sync-light' + (cls ? ' ' + cls : '');
    }

    // Debug: enable with  localStorage.setItem('videoSyncDebug','1')  then reload.
    // Logs to the console and shows the OCR crop (top-right) so you can check the
    // region lands on the element and see what's being read.
    function debugOn() { try { return localStorage.getItem('videoSyncDebug') === '1'; } catch (e) { return false; } }
    function dbg(...a) { if (debugOn()) console.log('[video-sync]', ...a); }
    function showDebugCrop(canvas, label) {
        if (!debugOn() || !canvas) return;
        let box = $('vsDebug');
        if (!box) {
            box = document.createElement('div'); box.id = 'vsDebug';
            box.style.cssText = 'position:fixed;top:8px;right:8px;z-index:2000;background:#000;'
                + 'border:1px solid #0f0;padding:4px;font:11px monospace;color:#0f0;text-align:center';
            document.body.appendChild(box);
        }
        const img = canvas.toDataURL();
        box.innerHTML = `<div>${label || ''}</div><img src="${img}" style="max-width:280px;display:block">`;
    }
    // Debug: the whole captured frame with the content rect (cyan) and the OCR
    // regions drawn, so you can see whether the boxes land on the elements.
    function showDebugFrame() {
        if (!debugOn() || !state.video) return;
        let box = $('vsDebug2');
        if (!box) {
            box = document.createElement('div'); box.id = 'vsDebug2';
            box.style.cssText = 'position:fixed;bottom:8px;right:8px;z-index:2000;background:#000;'
                + 'border:1px solid #0ff;padding:4px;font:11px monospace;color:#0ff';
            document.body.appendChild(box);
        }
        const v = state.video, w = 360, h = Math.round(360 * v.videoHeight / v.videoWidth);
        const c = document.createElement('canvas'); c.width = w; c.height = h;
        const ctx = c.getContext('2d'); ctx.drawImage(v, 0, 0, w, h);
        const cr = state.content, cw = cr.x1 - cr.x0, ch = cr.y1 - cr.y0;
        const rect = (reg, col) => {
            ctx.strokeStyle = col; ctx.lineWidth = 2;
            ctx.strokeRect((cr.x0 + reg[0] * cw) * w, (cr.y0 + reg[1] * ch) * h,
                           (reg[2] - reg[0]) * cw * w, (reg[3] - reg[1]) * ch * h);
        };
        ctx.strokeStyle = '#0ff'; ctx.lineWidth = 1;
        ctx.strokeRect(cr.x0 * w, cr.y0 * h, cw * w, ch * h);          // content rect
        if (isRace()) { rect(RACE_COUNTDOWN_REGION, 'yellow'); rect(sessionRegions()[0], 'lime'); }
        else rect(PQ_CLOCK_SWEEP, 'lime');
        box.innerHTML = '<div>frame · cyan=content yellow=countdown green=clock/lap</div>';
        box.appendChild(c);
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
                // Prefer a full monitor (so the fullscreen video fills the frame);
                // ≥10 fps to time the tick edge. The black-area crop (detectContentRect)
                // then trims letterbox bars / surrounding desktop down to the video.
                video: { displaySurface: 'monitor', frameRate: { ideal: 30 } }, audio: false,
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
        if (state.resumeTimer) { clearTimeout(state.resumeTimer); state.resumeTimer = null; }
        if (state.pausedBySync) {                   // don't leave playback frozen by a sync-pause
            const b = bus();
            if (b && typeof b.send === 'function') b.send({ cmd: 'play' });
            state.pausedBySync = false;
        }
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
            debug_file: '/dev/null',                // quiet Tesseract's core stats spam
        });
        state.worker = worker;
        return worker;
    }

    // Second worker for the wide pre-race countdown sweep: sparse-text mode reads
    // digits scattered anywhere in the crop (the single-line worker can't).
    async function ensureSparseWorker() {
        if (state.sparseWorker) return state.sparseWorker;
        if (typeof Tesseract === 'undefined') return null;
        const worker = await Tesseract.createWorker('eng');
        await worker.setParameters({
            tessedit_char_whitelist: '0123456789:',
            tessedit_pageseg_mode: '11',            // sparse text: find scattered digits
            debug_file: '/dev/null',
        });
        state.sparseWorker = worker;
        return worker;
    }

    // Third worker: reads the P/Q standings-tile HEADER line (session badge + time),
    // so it needs LETTERS as well as digits. psm 6 = a uniform multi-line block, so
    // data.text keeps the header and the standings rows on separate lines.
    async function ensureTextWorker() {
        if (state.textWorker) return state.textWorker;
        if (typeof Tesseract === 'undefined') return null;
        const worker = await Tesseract.createWorker('eng');
        await worker.setParameters({
            tessedit_char_whitelist: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:./ ',
            tessedit_pageseg_mode: '6',             // uniform block of text (multi-line)
            debug_file: '/dev/null',
        });
        state.textWorker = worker;
        return worker;
    }

    // The session badge that sits on the header line, by session type. Anchoring the
    // time read to this line keeps us off the standings rows (which carry lap times).
    function pqBadgeRx() {
        const t = ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase();
        return t === 'qualifying'
            ? /QUAL|SHOOT|\bS?Q\s?[123]\b/i         // QUALIFYING / SPRINT SHOOTOUT / Q1-3 / SQ1-3
            : /PRACT|\bFP\s?[123]\b/i;              // PRACTICE 1-3 / FP1-3
    }

    // Every MM:SS in the OCR text that could be a countdown (≤ CD_MAX), in ms.
    function cdCandidates(text) {
        const out = [], re = /(\d{1,2}):(\d{2})/g;
        let m;
        while ((m = re.exec(String(text))) !== null) {
            const min = +m[1], sec = +m[2];
            if (sec > 59) continue;
            const ms = (min * 60 + sec) * 1000;
            if (ms > 0 && ms <= CD_MAX_MS) out.push(ms);
        }
        return out;
    }

    // Every H:MM:SS / MM:SS in the OCR text, in ms (session clocks run up to ~2h).
    // Used to find the P/Q session timer inside the wide upper-left sweep.
    function timeCandidates(text) {
        const out = [], re = /(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?:\.(\d))?/g;
        let m;
        while ((m = re.exec(String(text))) !== null) {
            const h = m[1] ? +m[1] : 0, mn = +m[2], s = +m[3], t = m[4] ? +m[4] : 0;
            const ms = (((h * 60 + mn) * 60 + s) * 10 + t) * 100;
            if (ms > 0 && ms <= 120 * 60 * 1000) out.push(ms);
        }
        return out;
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
        const bright = (x, y) => { const i = (y * w + x) * 4; return (0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]) > 32; };
        // A row/col is "video" only if a fraction of it is non-black — robust to a
        // video that doesn't fill the captured frame (shared whole/extended
        // desktop) and to stray bright pixels, unlike per-pixel edge trimming.
        const FR = 0.06;
        const rowOn = [], colOn = [];
        for (let y = 0; y < h; y++) { let c = 0; for (let x = 0; x < w; x++) if (bright(x, y)) c++; rowOn[y] = c / w >= FR; }
        for (let x = 0; x < w; x++) { let c = 0; for (let y = 0; y < h; y++) if (bright(x, y)) c++; colOn[x] = c / h >= FR; }
        let top = 0; while (top < h && !rowOn[top]) top++;
        let bot = h - 1; while (bot > top && !rowOn[bot]) bot--;
        let left = 0; while (left < w && !colOn[left]) left++;
        let right = w - 1; while (right > left && !colOn[right]) right--;
        if (bot - top < h * 0.3 || right - left < w * 0.3) return { x0: 0, y0: 0, x1: 1, y1: 1 };
        return { x0: left / w, y0: top / h, x1: (right + 1) / w, y1: (bot + 1) / h };
    }

    const _c = document.createElement('canvas');
    function cropToCanvas(region, opts) {
        const v = state.video;
        if (!region || !v) return null;
        const up = (opts && opts.upscale) || OCR_UPSCALE;
        const mode = (opts && opts.mode) || 'otsu';
        const c = state.content;                    // content-relative → frame fractions
        const cw = c.x1 - c.x0, ch = c.y1 - c.y0;
        const fx0 = c.x0 + region[0] * cw, fy0 = c.y0 + region[1] * ch;
        const fx1 = c.x0 + region[2] * cw, fy1 = c.y0 + region[3] * ch;
        const sx = fx0 * v.videoWidth, sy = fy0 * v.videoHeight;
        const sw = (fx1 - fx0) * v.videoWidth, sh = (fy1 - fy0) * v.videoHeight;
        if (sw < 1 || sh < 1) return null;
        _c.width = Math.round(sw * up);
        _c.height = Math.round(sh * up);
        const ctx = _c.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(v, sx, sy, sw, sh, 0, 0, _c.width, _c.height);
        const img = ctx.getImageData(0, 0, _c.width, _c.height);
        const d = img.data, n = d.length / 4;
        if (mode === 'white') {
            // Keep only near-white pixels (broadcast captions are white) → black on
            // white. For a WIDE crop over a mixed bright/dark scene, global Otsu
            // fails (bright track merges with white text); white-isolation doesn't.
            for (let i = 0; i < d.length; i += 4) {
                const mn = Math.min(d[i], d[i + 1], d[i + 2]);
                const px = mn > WHITE_THR ? 0 : 255;
                d[i] = d[i + 1] = d[i + 2] = px; d[i + 3] = 255;
            }
        } else {
            // Otsu auto-threshold over this crop's luma histogram (tight single-
            // element regions), then binarise light text → dark on white.
            const hist = new Array(256).fill(0), lum = new Uint8Array(n);
            for (let i = 0, j = 0; i < d.length; i += 4, j++) {
                const l = (0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]) | 0;
                lum[j] = l; hist[l]++;
            }
            let sum = 0; for (let t = 0; t < 256; t++) sum += t * hist[t];
            let sumB = 0, wB = 0, maxVar = -1, thr = LUMA_THRESHOLD;
            for (let t = 0; t < 256; t++) {
                wB += hist[t]; if (!wB) continue;
                const wF = n - wB; if (!wF) break;
                sumB += t * hist[t];
                const mB = sumB / wB, mF = (sum - sumB) / wF, vv = wB * wF * (mB - mF) * (mB - mF);
                if (vv > maxVar) { maxVar = vv; thr = t; }
            }
            for (let i = 0, j = 0; i < d.length; i += 4, j++) {
                const px = lum[j] > thr ? 0 : 255;
                d[i] = d[i + 1] = d[i + 2] = px; d[i + 3] = 255;
            }
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
        dbg('content rect', JSON.stringify(state.content), 'video',
            state.video && (state.video.videoWidth + 'x' + state.video.videoHeight));
        if (isRace()) { resetRace(); runRaceLoop(); }   // lap-increment sync
        else { toAcquire(); runLoop(); }                // P/Q clock tick sync
    }

    async function runLoop() {
        if (!state.active) return;
        // Hidden tab/window → timers throttle and the capture freezes, so reads
        // are stale. Idle until visible, then re-acquire the tick phase cleanly.
        if (document.hidden) { toAcquire(); state.ocrTimer = setTimeout(runLoop, 1000); return; }
        showDebugFrame();

        const now = performance.now();
        // Read the standings-tile HEADER line — the session badge + time remaining /
        // countdown, which sit just below the FIA logo and ABOVE the standings rows.
        // OCR the wide upper-left block as text, split into lines, and take the time
        // from the line carrying the session badge. The standings rows (TLA + lap time
        // + gap) carry their own MM:SS-looking values, so we anchor on the badge line,
        // not "any time on screen". Fallback: nearest-data if the badge is unreadable.
        let raw = null;
        const sweep = cropToCanvas(PQ_CLOCK_SWEEP, { upscale: 2 });
        const tw = await ensureTextWorker();
        if (sweep && tw) {
            showDebugCrop(sweep, 'header sweep');
            try {
                const { data } = await tw.recognize(sweep);
                const lines = (data.text || '').split('\n').map(s => s.trim()).filter(Boolean);
                const badgeRe = pqBadgeRx();
                let badge = null;
                for (const ln of lines) {                       // header line = badge + its time
                    if (!badgeRe.test(ln)) continue;
                    const c = timeCandidates(ln);
                    if (c.length) { raw = c[0]; badge = ln; break; }
                }
                if (raw == null) {                              // badge line unreadable → nearest-data fallback
                    const all = timeCandidates(data.text || ''), dRef = dataClockMs();
                    if (all.length && dRef != null) {
                        let best = null, bd = Infinity;
                        for (const c of all) { const dd = Math.abs(c - dRef); if (dd < bd) { bd = dd; best = c; } }
                        raw = bd <= PQ_MATCH_TOL_MS ? best : null;
                    }
                }
                dbg('header lines', JSON.stringify(lines), 'badge', JSON.stringify(badge),
                    '→ raw', raw != null ? (raw / 1000).toFixed(1) + 's' : null);
            } catch (e) { /* transient OCR error */ }
        }
        if (!state.active) return;

        const tvMs = acceptTv(raw, now);
        if (tvMs == null) state.nullStreak++; else state.nullStreak = 0;
        const dMs = dataClockMs();
        const inSync = (tvMs != null && dMs != null) && Math.abs(tvMs - dMs) <= INSYNC_S * 1000;

        correct(tvMs, dMs, now, inSync);

        // De-sync drifts slowly and the wide OCR crop is heavy, so sample sparsely
        // (~5 s). Search a little faster until we have a first reference read.
        const interval = state.lastTvMs == null ? 2000 : OCR_INTERVAL_MS;
        state.ocrTimer = setTimeout(runLoop, interval);
    }

    // Bring the data+audio to the TV's clock position. Both clocks count DOWN (time
    // remaining), so dMs < tvMs ⇒ data is AHEAD of the TV, dMs > tvMs ⇒ behind.
    // Strategy (cheap pauses over laggy seeks):
    //   • data AHEAD  → PAUSE and let the TV catch up; never seek backward.
    //   • data BEHIND → seek forward to TV + 5 s (overshoot) so we land ahead and the
    //                   pause-branch fine-syncs back. If TV + 5 s is past the live
    //                   edge → go to live; if already at live and still behind → hold.
    //   green = in sync   yellow = correcting / can't catch up   red = no read
    function correct(tvMs, dMs, now, inSync) {
        if (tvMs == null || dMs == null) {
            setLight(state.lastTvMs != null ? 'ok' : 'adjust');
            return;
        }
        if (inSync) { resumeData(); state.jumpFrom = null; setLight('ok'); return; }
        if (now < state.cooldownUntil) { setLight('adjust'); return; }   // let a pause / seek settle

        const cur = currentOffset();
        if (cur == null) { setLight('error'); return; }

        const aheadS = (tvMs - dMs) / 1000;         // + ⇒ data ahead of the TV
        if (aheadS > 0) {
            // DATA AHEAD → pause and resume once the TV catches up (~aheadS seconds at
            // 1× real time). No backward seek.
            const waitS = Math.min(aheadS, MAX_PAUSE_S);
            pauseData();
            if (state.resumeTimer) clearTimeout(state.resumeTimer);
            state.resumeTimer = setTimeout(resumeData, waitS * 1000);
            state.cooldownUntil = now + waitS * 1000 + SEEK_COOLDOWN_MS;
            state.jumpFrom = null;
            setLight('adjust');
            return;
        }

        // DATA BEHIND → move forward. If a prior forward seek didn't advance, we're
        // pinned at the live edge with the TV ahead → nothing to do, hold yellow.
        if (state.jumpFrom != null && (cur - state.jumpFrom) < 0.5) { setLight('adjust'); return; }
        const target = cur + (-aheadS) + FWD_OVERSHOOT_S;   // TV position + 5 s overshoot
        state.jumpFrom = cur;
        if (state.maxOffset != null && target >= state.maxOffset) {
            seekLiveCmd();                          // can't reach TV + 5 s → jump to live edge
        } else {
            seekTo(target);                         // skip to TV + 5 s
        }
        state.cooldownUntil = now + SEEK_COOLDOWN_MS;
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
        state.offLapStreak = 0; state.cooldownUntil = 0; state.cdAnchors = [];
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
        if (Math.abs(offsetS) < INSYNC_S) { setLight('ok'); return; }   // within deadband → don't re-sync
        seekTo(cur + offsetS);
        state.cooldownUntil = now + SEEK_COOLDOWN_MS;
        setLight('adjust');
    }

    // Pre-race: OCR the countdown (which counts down to the SCHEDULED start, not
    // lights-out) and coarse-align the data to the broadcast wall-clock:
    //   broadcast_now = scheduled_start − countdown  →  seek data there.
    // The data clock is UTC, so the target offset = broadcast_now − data_start.
    async function tryCountdownAlign(now) {
        const b = bus();
        if (scheduledStartMs == null) { dbg('countdown skip: scheduledStartMs unknown (no startDate on sessionInfo — rebuild/reconnect?)'); return false; }
        if (!b || !b.clockTime || !b.startTime) { dbg('countdown skip: clock not ready'); return false; }
        if (b.clockTime.getTime() > scheduledStartMs + 5000) { dbg('countdown skip: past scheduled start'); return false; }
        const worker = await ensureSparseWorker();
        if (!worker) return false;
        const canvas = cropToCanvas(RACE_COUNTDOWN_REGION, { upscale: 2, mode: 'white' });
        if (!canvas) return false;
        showDebugCrop(canvas, 'countdown sweep');
        let txt = '';
        try { const { data } = await worker.recognize(canvas); txt = (data.text || '').trim(); }
        catch (e) { dbg('countdown OCR error', e); return false; }
        const cands = cdCandidates(txt);
        dbg('countdown sweep:', JSON.stringify(txt.replace(/\s+/g, ' ')), '→ cand(s)', cands.map(c => (c / 1000).toFixed(0)));
        if (!cands.length) { state.cdAnchors = []; return false; }   // nothing time-like → defer to lap/ENTER

        // Match candidates against tracked anchors. A real countdown ticks DOWN at
        // wall-clock rate, so the observed value should equal (anchor − elapsed);
        // confirm one only after it has tracked for CD_CONFIRM_MS (static numbers
        // diverge from anchor−elapsed and get dropped before they can confirm).
        let confirmed = null;
        const next = [];
        for (const a of state.cdAnchors) {
            const exp = a.val - (now - a.at);
            const hit = cands.find(c => Math.abs(c - exp) <= CD_MATCH_TOL_MS);
            if (hit == null) continue;                              // anchor's countdown vanished → drop
            next.push(a);                                           // keep ORIGINAL val/at (measures total drop)
            if (now - a.at >= CD_CONFIRM_MS) confirmed = hit;       // tracked long enough → trust it
        }
        for (const c of cands) {                                    // seed anchors for untracked candidates
            if (!next.some(a => Math.abs((a.val - (now - a.at)) - c) <= CD_MATCH_TOL_MS)) next.push({ val: c, at: now });
        }
        state.cdAnchors = next;

        if (confirmed == null) { setLight('arming'); return true; } // still verifying which number is the countdown

        const cur = b.getCurrentOffset();
        const target = (scheduledStartMs - confirmed - b.startTime.getTime()) / 1000;
        dbg('countdown CONFIRMED', (confirmed / 1000).toFixed(0), 's → target', target.toFixed(1), 'cur', cur.toFixed(1), 'Δ', (cur - target).toFixed(1));
        if (Math.abs(cur - target) <= 1.5) { setLight('ok'); return true; }   // aligned → green
        if (target >= 0 && now >= state.cooldownUntil) {
            seekTo(target); state.cooldownUntil = now + SEEK_COOLDOWN_MS; dbg('countdown SEEK →', target.toFixed(1));
        }
        setLight('adjust');
        return true;
    }

    async function runRaceLoop() {
        if (!state.active) return;
        if (document.hidden) { state.ocrTimer = setTimeout(runRaceLoop, 1000); return; }
        showDebugFrame();

        const now = performance.now();
        const b = bus();
        const cur = (b && typeof b.getCurrentOffset === 'function') ? b.getCurrentOffset() : null;
        const schedOff = (b && b.startTime && scheduledStartMs != null)
            ? (scheduledStartMs - b.startTime.getTime()) / 1000 : null;
        const lightsOff = raceStartMs != null ? raceStartMs / 1000 : null;

        // Phase gating. The FORMATION LAP (scheduled start → lights-out) is a sync
        // dead zone: the upper-left graphics churn and the lap counter isn't live,
        // so any OCR there only causes false de-syncs. We HOLD (no OCR, assume in
        // sync) through it, and resume sync via LAP NUMBERS only after lights-out.
        // Boundaries: scheduledStartMs (sessionInfo) + raceStartMs (GREEN event).
        if (cur != null && schedOff != null && lightsOff != null && cur >= schedOff && cur < lightsOff) {
            setLight('ok');
            state.ocrTimer = setTimeout(runRaceLoop, 500);
            return;
        }
        // Before lights-out: pre-race countdown align ONLY (never lap OCR — the lap
        // region churns pre-race). After lights-out: fall through to lap-num sync.
        const beforeLights = (lightsOff != null) ? (cur != null && cur < lightsOff)
                                                 : (schedOff != null && cur != null && cur < schedOff);
        if (beforeLights) {
            if (await tryCountdownAlign(now)) {       // pre-race coarse alignment
                if (!state.active) return;
                state.ocrTimer = setTimeout(runRaceLoop, 250);   // fast: anchors track the tick-down
                return;
            }
            if (!state.active) return;
            setLight('adjust');                       // searching for the countdown (use ENTER if absent)
            state.ocrTimer = setTimeout(runRaceLoop, 300);
            return;
        }
        if (!state.active) return;
        let lap = null;
        const canvas = cropToCanvas(sessionRegions()[0]);   // lap-counter region
        if (canvas) {
            showDebugCrop(canvas, 'lap region');
            try { const { data } = await state.worker.recognize(canvas); lap = parseLap(data.text); dbg('lap OCR:', JSON.stringify((data.text || '').trim()), '→', lap); }
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

    // ── Start / restart anchors (ENTER) ──────────────────────────────────
    // GREEN markers from session:events = session starts + restarts (after red
    // flags). For a race the FIRST green is lights-out; for P/Q each green is a
    // part start or a restart.
    let raceStartMs = null;        // race lights-out (first green)
    let greenOffsetsMs = [];       // all green offsets, ascending
    messageBus.on('session:events', (events) => {
        if (!Array.isArray(events)) return;
        greenOffsetsMs = events
            .filter(e => String((e.data && (e.data.event || e.data)) || '').toUpperCase() === 'GREEN')
            .map(e => e.offset_ms)
            .filter(o => typeof o === 'number')
            .sort((a, b) => a - b);
        raceStartMs = greenOffsetsMs.length ? greenOffsetsMs[0] : null;
    });
    // Scheduled session start (formation-lap start), UTC ms — from sessionInfo.
    let scheduledStartMs = null;
    function parseGmt(s) {
        const m = String(s || '').match(/(-?)(\d+):(\d+):(\d+)/);
        return m ? (m[1] === '-' ? -1 : 1) * ((+m[2] * 3600 + +m[3] * 60 + +m[4]) * 1000) : 0;
    }
    messageBus.on('sessionInfo', (d) => {
        if (d && d.startDate) {
            const naive = Date.parse(/[zZ]$/.test(d.startDate) ? d.startDate : d.startDate + 'Z');
            if (!isNaN(naive)) scheduledStartMs = naive - parseGmt(d.gmtOffset);
        }
        dbg('sessionInfo startDate=', d && d.startDate, 'gmt=', d && d.gmtOffset, '→ scheduledStartMs', scheduledStartMs);
    });
    // Seek with lag compensation — ENTER / "+" only (bias the target ahead so the
    // server's fetch lag doesn't undershoot the requested instant).
    function seekAhead(offsetS) { seekToOffset(Math.max(0, offsetS + LAG_COMP_S)); }

    // ENTER — jump to the next start / restart.
    //   Practice / Qualifying: the next GREEN (part start, or restart after a red
    //     flag) AFTER the current position.
    //   Race: > 1 min before lights-out (or no green known yet) → scheduled start
    //     (formation lap); from there through the end of lap 1 → lights-out;
    //     lap 2 onward → no-op (lap-increment sync owns it).
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.isComposing) return;
        if (typeof seekToOffset !== 'function') return;
        const b = bus();
        if (!b || !b.startTime || typeof b.getCurrentOffset !== 'function') return;
        const cur = b.getCurrentOffset();

        if (isRace()) {
            if (state.dataLap != null && state.dataLap >= 2) return;   // lap 2+ → disabled
            const greenS = raceStartMs != null ? raceStartMs / 1000 : null;
            const schedS = scheduledStartMs != null
                ? (scheduledStartMs - b.startTime.getTime()) / 1000 : null;
            if (greenS == null || cur < greenS - 60) {
                if (schedS != null) { e.preventDefault(); seekAhead(schedS); }   // → scheduled start
            } else {
                e.preventDefault(); seekAhead(greenS);                            // → lights-out
            }
            return;
        }
        // Practice / Qualifying: jump to the next green/restart ahead of us.
        const next = greenOffsetsMs.find(o => o / 1000 > cur + 1);
        if (next != null) { e.preventDefault(); seekAhead(next / 1000); }
    });

    // Manual fine nudges while video sync is active:
    //   "+" / "=" : TV ahead → skip the data forward ~0.5 s (with lag comp).
    //   "−"       : TV behind → pause ~0.1 s and resume so the TV catches up
    //               (no seek → unaffected by server response time).
    document.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.isComposing) return;
        if (!state.active) return;
        const b = bus();
        if (e.key === '+' || e.key === '=') {
            e.preventDefault();
            const cur = (b && typeof b.getCurrentOffset === 'function') ? b.getCurrentOffset() : null;
            if (cur != null) seekTo(cur + 0.5 + LAG_COMP_S);
        } else if (e.key === '-') {
            e.preventDefault();
            if (b && b.isPlaying && typeof b.send === 'function') {
                b.send({ cmd: 'pause' });
                setTimeout(() => b.send({ cmd: 'play' }), 100);
            }
        }
    });

    window.toggleVideoSync = toggle;
})();
