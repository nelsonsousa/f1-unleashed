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

    // P/Q sync cadence + tick-phase pinning. On (re)locate we burst back-to-back on
    // a TIGHT crop of the clock (cached bbox → cheap OCR) to pin the exact tick
    // instant (~0.1 s), then sample once per second just AFTER each predicted tick.
    // Once in sync we relax to every 5 s. On desync we drop back to 1 s.
    const ACTIVE_INTERVAL_MS = 1000;    // 1 s cadence while searching / correcting
    const SYNCED_INTERVAL_MS = 5000;    // relaxed cadence once in sync
    const PIN_BURST_MS = 1200;          // burst this long to catch a 1 s tick edge
    const PIN_OFFSET_MS = 120;          // sample this far after the predicted tick
    const RELOCATE_AFTER = 4;           // tight-crop misses in a row → wide re-locate

    // Within INSYNC_S we treat it as synced and do NOTHING; beyond it we correct,
    // but only TO within CORRECT_TARGET_S (tighter) so we don't pause/seek-thrash
    // around the 1 s boundary (detect loose, correct tight).
    const INSYNC_S = 1.0;
    const CORRECT_TARGET_S = 0.3;
    // The server takes a moment to fetch the exact instant, so seeks land a touch
    // short. Bias ENTER / "+" jumps this far AHEAD (manual "−" / scrubber unaffected).
    const LAG_COMP_S = 0.5;

    const SEEK_COOLDOWN_MS = 800;       // suppress corrections until a pause/seek settles
    const FWD_OVERSHOOT_S = 2;          // base forward-seek overshoot (TV + this); adaptive on top
    const MAX_PAUSE_S = 20;             // cap a single pause-to-resync hold

    const state = {
        active: false,
        stream: null,
        video: null,                                // hidden <video> of the stream
        content: { x0: 0, y0: 0, x1: 1, y1: 1 },     // 16:9 video rect within the frame (letterbox-aware)
        worker: null,                               // psm-7 tight single-line (clock / lap)
        sparseWorker: null,                         // psm-11 wide countdown sweep (race)
        textWorker: null,                           // psm-6 alphanumeric header sweep (P/Q locate)
        cdAnchors: [],                              // [{val,at}] race countdown candidates
        ocrTimer: null,
        // ── P/Q clock sync ──
        phase: 'search',                            // 'search' | 'pin' | 'monitor'
        clockBox: null,                             // cached [x0,y0,x1,y1] of the time text (content fractions)
        clockWide: false,                           // true = no tight bbox; fall back to wide badge-line OCR
        tightMiss: 0,                               // consecutive tight-crop misses → re-locate
        pinStart: 0, pinPrevSec: null,              // tick-pinning burst state
        tvTickAt: null, tvTickSec: null,            // pinned tick: perf time + whole-second value
        synced: false,                              // in-sync (drives the relaxed cadence)
        seekLagS: 0,                                // measured forward-seek shortfall (adaptive overshoot)
        measuringSeek: null,                        // {at} pending post-seek residual measure
        lastTvMs: null, lastTvAt: 0,                // last accepted TV read
        jumpFrom: null,                             // data offset at last forward-seek (live-edge probe)
        cooldownUntil: 0,                           // suppress corrections until this perf time
        pausedBySync: false,                        // we paused playback to let the TV catch up
        resumeTimer: null,                          // pending resume after a sync-pause
        maxOffset: null,                            // live edge / total available offset (s)
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

    // Footer message (status bar): a red instruction shown only when the TV is
    // ahead of our live edge and we CAN'T catch up — tells the user how long to
    // pause their TV. Cleared the moment we're back in sync.
    function setFooterMsg(text) {
        let el = $('videoSyncMsg');
        if (!el) {
            const foot = $('statusFooter');
            if (!foot) return;
            el = document.createElement('span');
            el.id = 'videoSyncMsg';
            el.style.cssText = 'color:#ff4d4d;font-weight:600;margin-left:12px';
            foot.appendChild(el);
        }
        if (el.textContent !== text) el.textContent = text;
    }
    function clearFooterMsg() {
        const el = $('videoSyncMsg');
        if (el && el.textContent) el.textContent = '';
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

    // Flatten Tesseract word boxes (output shape varies by version: words / blocks /
    // lines). Each entry is {text, bbox:{x0,y0,x1,y1}} in the recognised image's px.
    function collectWords(data) {
        const out = [];
        const push = (w) => { if (w && w.text && w.bbox) out.push({ text: String(w.text).trim(), bbox: w.bbox }); };
        if (Array.isArray(data.words)) data.words.forEach(push);
        if (Array.isArray(data.blocks)) data.blocks.forEach(b =>
            (b.paragraphs || []).forEach(p => (p.lines || []).forEach(l => (l.words || []).forEach(push))));
        if (Array.isArray(data.lines)) data.lines.forEach(l => (l.words || []).forEach(push));
        return out;
    }
    // Map a word bbox (px in the sweep canvas) back to content-rect fractions, padded
    // (the clock changes width as digits change, e.g. "1:00:00" vs "9:59").
    function boxToContentRect(bbox, region, cw, ch) {
        const [rx0, ry0, rx1, ry1] = region;
        const fx0 = rx0 + (bbox.x0 / cw) * (rx1 - rx0);
        const fy0 = ry0 + (bbox.y0 / ch) * (ry1 - ry0);
        const fx1 = rx0 + (bbox.x1 / cw) * (rx1 - rx0);
        const fy1 = ry0 + (bbox.y1 / ch) * (ry1 - ry0);
        const pw = (fx1 - fx0) * 0.8 + 0.005, ph = (fy1 - fy0) * 0.5 + 0.006;
        return [Math.max(0, fx0 - pw), Math.max(0, fy0 - ph),
                Math.min(1, fx1 + pw), Math.min(1, fy1 + ph)];
    }

    // SEARCH / RE-LOCATE: wide header OCR, anchored on the session badge line, finds
    // the time and (when possible) CACHES its bounding box for fast tight reads.
    // Returns {ms, box, wide} or null when no clock is on screen.
    async function locateClock() {
        const sweep = cropToCanvas(PQ_CLOCK_SWEEP, { upscale: 2 });
        const tw = await ensureTextWorker();
        if (!sweep || !tw) return null;
        showDebugCrop(sweep, 'locate sweep');
        let data;
        try { ({ data } = await tw.recognize(sweep, {}, { blocks: true })); }
        catch (e) { return null; }
        const lines = (data.text || '').split('\n').map(s => s.trim()).filter(Boolean);
        const badgeRe = pqBadgeRx();
        const badgeLine = lines.find(ln => badgeRe.test(ln) && timeCandidates(ln).length);
        const dRef = dataClockMs();
        let textMs = null;
        if (badgeLine) {
            textMs = timeCandidates(badgeLine)[0];
        } else {                                          // no badge → nearest-data across all times
            const all = timeCandidates(data.text || '');
            if (all.length && dRef != null) {
                let b = null, bd = Infinity;
                for (const c of all) { const dd = Math.abs(c - dRef); if (dd < bd) { bd = dd; b = c; } }
                if (bd <= PQ_MATCH_TOL_MS) textMs = b;
            }
        }
        if (textMs == null) return null;                  // clock not visible
        // Try to pin a TIGHT bbox for that time (fast subsequent reads); else fall
        // back to wide badge-line reads (functional, just slower — no tick pinning).
        const timeRe = /(?:\d{1,2}:)?\d{1,2}:\d{2}/;
        let bestW = null, bestD = Infinity;
        for (const w of collectWords(data)) {
            const ms = timeRe.test(w.text) ? parseClock(w.text) : null;
            if (ms == null) continue;
            const dd = Math.abs(ms - textMs);
            if (dd < bestD) { bestD = dd; bestW = w; }
        }
        if (bestW && bestD <= 1500) {
            const box = boxToContentRect(bestW.bbox, PQ_CLOCK_SWEEP, sweep.width, sweep.height);
            dbg('locate TIGHT', (textMs / 1000).toFixed(1), 's box', box.map(n => n.toFixed(3)));
            return { ms: textMs, box, wide: false };
        }
        dbg('locate WIDE (no bbox)', (textMs / 1000).toFixed(1), 's');
        return { ms: textMs, box: PQ_CLOCK_SWEEP, wide: true };
    }

    // Fast read of the cached clock box. Tight = single-line digit OCR on the small
    // crop; wide = the badge-line fallback. Returns ms or null (clock not readable).
    async function readTight() {
        if (!state.clockBox) return null;
        if (state.clockWide) {
            const sweep = cropToCanvas(PQ_CLOCK_SWEEP, { upscale: 2 });
            const tw = await ensureTextWorker();
            if (!sweep || !tw) return null;
            showDebugCrop(sweep, 'clock wide');
            try {
                const { data } = await tw.recognize(sweep);
                const badgeRe = pqBadgeRx();
                for (const ln of (data.text || '').split('\n')) {
                    if (badgeRe.test(ln)) { const c = timeCandidates(ln); if (c.length) return c[0]; }
                }
                const all = timeCandidates(data.text || ''), dRef = dataClockMs();
                if (all.length && dRef != null) {
                    let b = null, bd = Infinity;
                    for (const c of all) { const dd = Math.abs(c - dRef); if (dd < bd) { bd = dd; b = c; } }
                    return bd <= PQ_MATCH_TOL_MS ? b : null;
                }
                return null;
            } catch (e) { return null; }
        }
        const crop = cropToCanvas(state.clockBox, { upscale: 3 });
        const w = await ensureWorker();
        if (!crop || !w) return null;
        showDebugCrop(crop, 'clock tight');
        try {
            const { data } = await w.recognize(crop);
            const ms = parseClock(data.text);
            dbg('tight', JSON.stringify((data.text || '').trim()), '→', ms != null ? (ms / 1000).toFixed(1) : null);
            return ms;
        } catch (e) { return null; }
    }

    // Schedule the next P/Q sample, phase-aligned just AFTER the predicted TV tick.
    function schedulePQ(minDelayMs) {
        let delay = minDelayMs;
        if (state.tvTickAt != null) {
            const target = performance.now() + minDelayMs;
            const k = Math.ceil((target - state.tvTickAt - PIN_OFFSET_MS) / 1000);
            delay = Math.max(20, state.tvTickAt + k * 1000 + PIN_OFFSET_MS - performance.now());
        }
        state.ocrTimer = setTimeout(runPQ, delay);
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
        else { state.phase = 'search'; state.clockBox = null; runPQ(); }   // P/Q clock sync
    }

    // P/Q state machine: SEARCH (locate + cache the clock box) → PIN (burst to pin
    // the tick instant) → MONITOR (1 s, assess + correct; relax to 5 s once synced).
    async function runPQ() {
        if (!state.active) return;
        if (document.hidden) {              // hidden tab throttles timers → reads stale; idle + re-pin
            state.phase = state.clockBox ? 'pin' : 'search'; state.tvTickAt = null;
            state.ocrTimer = setTimeout(runPQ, 1000); return;
        }
        showDebugFrame();
        const now = performance.now();

        // ── SEARCH / RE-LOCATE ──
        if (state.phase === 'search' || !state.clockBox) {
            const loc = await locateClock();
            if (!state.active) return;
            if (!loc) {                     // clock not on screen → assume in sync, keep looking
                setLight(state.lastTvMs != null ? 'ok' : 'adjust');
                state.ocrTimer = setTimeout(runPQ, ACTIVE_INTERVAL_MS); return;
            }
            state.clockBox = loc.box; state.clockWide = !!loc.wide; state.tightMiss = 0;
            if (state.clockWide) { state.phase = 'monitor'; state.tvTickAt = null; }
            else { state.phase = 'pin'; state.pinStart = now; state.pinPrevSec = null; state.tvTickAt = null; }
            state.ocrTimer = setTimeout(runPQ, state.clockWide ? ACTIVE_INTERVAL_MS : 0); return;
        }

        // Read the (cached) clock.
        const tv = await readTight();
        if (!state.active) return;
        if (tv == null) {                   // clock briefly gone (replay/ad/misread) → hold, re-locate if persistent
            if (++state.tightMiss >= RELOCATE_AFTER) { state.phase = 'search'; state.clockBox = null; }
            setLight(state.lastTvMs != null ? 'ok' : 'adjust');
            schedulePQ(state.synced ? SYNCED_INTERVAL_MS : ACTIVE_INTERVAL_MS); return;
        }
        state.tightMiss = 0;
        const sec = Math.round(tv / 1000);

        // ── PIN: burst back-to-back until we catch a 1 s tick edge AND ~1.2 s passed ──
        if (state.phase === 'pin') {
            if (state.pinPrevSec != null && sec === state.pinPrevSec - 1) { state.tvTickAt = now; state.tvTickSec = sec; }
            state.pinPrevSec = sec;
            state.lastTvMs = tv; state.lastTvAt = now;
            if (state.tvTickAt == null || (now - state.pinStart) < PIN_BURST_MS) { state.ocrTimer = setTimeout(runPQ, 0); return; }
            state.phase = 'monitor';
            dbg('pinned tick @', state.tvTickSec, 's');
        }

        // ── MONITOR: assess desync + correct ──
        state.lastTvMs = tv; state.lastTvAt = now;
        const dMs = dataClockMs();
        if (dMs == null) { setLight('error'); schedulePQ(ACTIVE_INTERVAL_MS); return; }

        // Adaptive overshoot: once a prior forward seek has SETTLED (past cooldown),
        // if it left us still (modestly) behind we undershot — overshoot a touch more
        // next time (bounded). Large residuals (live edge) are ignored.
        if (state.measuringSeek && now >= state.cooldownUntil) {
            const stillBehind = (dMs - tv) / 1000;      // + ⇒ data still behind the TV
            if (stillBehind > CORRECT_TARGET_S && stillBehind < 3) state.seekLagS = Math.min(3, state.seekLagS + stillBehind * 0.5);
            else if (stillBehind < -1) state.seekLagS = Math.max(0, state.seekLagS - 0.5);
            state.measuringSeek = null;
        }

        const desyncS = Math.abs(tv - dMs) / 1000;
        if (desyncS <= INSYNC_S) {                       // step 4-ok / step 6 (<1 s → do nothing)
            resumeData(); clearFooterMsg(); state.jumpFrom = null; state.measuringSeek = null; state.synced = true; setLight('ok');
            schedulePQ(SYNCED_INTERVAL_MS); return;       // step 5: relax to 5 s
        }
        state.synced = false;
        correctPQ(tv, dMs, now);                          // step 4: pause / overshoot / live-edge
        schedulePQ(ACTIVE_INTERVAL_MS);
    }

    // Step 4 correction. Both clocks count DOWN: tv>d ⇒ data AHEAD of TV; tv<d ⇒ behind.
    //   4.1 data AHEAD  → pause, resume when the TV catches up (never seek backward).
    //   4.2 data BEHIND → if reachable, jump to TV + overshoot (the pause branch then
    //       fine-trims); if the TV is beyond our live edge, hold yellow + tell the
    //       user how long to pause their TV.
    function correctPQ(tvMs, dMs, now) {
        if (now < state.cooldownUntil) { setLight('adjust'); return; }   // let a pause / seek settle
        const cur = currentOffset();
        if (cur == null) { setLight('error'); return; }
        const aheadS = (tvMs - dMs) / 1000;          // + ⇒ data ahead of the TV

        if (aheadS > 0) {                            // 4.1 data ahead → pause + wait
            clearFooterMsg(); state.jumpFrom = null;
            const waitS = Math.min(Math.max(0, aheadS - CORRECT_TARGET_S), MAX_PAUSE_S);
            if (waitS < 0.1) { setLight('ok'); return; }
            pauseData();
            if (state.resumeTimer) clearTimeout(state.resumeTimer);
            state.resumeTimer = setTimeout(resumeData, waitS * 1000);
            state.cooldownUntil = now + waitS * 1000 + SEEK_COOLDOWN_MS;
            setLight('adjust'); return;
        }

        // 4.2 data behind → forward.
        const behindS = -aheadS;
        // 4.2.2 a prior forward seek didn't advance ⇒ pinned at the live edge, TV is
        // ahead of the stream → can't catch up; instruct the user to pause their TV.
        if (state.jumpFrom != null && (cur - state.jumpFrom) < 0.5) {
            setFooterMsg(`TV ahead. Pause video ${Math.max(1, Math.round(behindS))} seconds`);
            setLight('adjust'); return;
        }
        // 4.2.1 reachable → jump to TV + overshoot (adaptive), clamped to the live edge.
        clearFooterMsg();
        const overshoot = FWD_OVERSHOOT_S + Math.max(0, state.seekLagS);
        let target = cur + behindS + overshoot;
        if (state.maxOffset != null) target = Math.min(target, state.maxOffset);
        state.jumpFrom = cur; state.measuringSeek = { at: now };
        seekTo(target);
        state.cooldownUntil = now + SEEK_COOLDOWN_MS;
        setLight('adjust');
    }

    // Becoming visible again: re-pin the tick phase from scratch (P/Q only).
    document.addEventListener('visibilitychange', () => {
        if (state.active && !document.hidden && !isRace()) {
            state.phase = state.clockBox ? 'pin' : 'search';
            state.pinStart = performance.now(); state.pinPrevSec = null; state.tvTickAt = null;
        }
    });

    // ── Race lap-sync ────────────────────────────────────────────────────
    // Pre-race relies on the countdown graphic (only present ~20 min out) + the
    // user's ENTER (formation-lap start, then lights-out). Once racing we already
    // start ~synced, so the only re-sync opportunity is each LAP increment: a
    // steady 1 s cycle OCRs the leader lap and aligns the data lap-cross to the TV's.
    // No bursting for sub-second lap-cross accuracy — 1 s is enough and chasing it
    // tends to backfire. Losing sync mid-lap is acceptable; the next lap re-aligns.

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

        // Steady 1 s cycle — re-sync happens on each lap increment (tryAlign).
        state.ocrTimer = setTimeout(runRaceLoop, 1000);
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
