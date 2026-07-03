/**
 * Video sync (Trello: 3-video-sync) — ON-DEMAND one-shot.
 *
 * The TV broadcast is watched on mute alongside the (already-synced) data +
 * commentary. Click the "Video sync" button to align playback to the TV
 * picture: we briefly screen-share, OCR the on-screen clock (P/Q) or lap
 * counter (race), seek the data ONCE to match, then RELEASE the capture — so
 * there is ZERO ongoing cost between syncs. Both clocks advance at 1×, so once
 * aligned they stay aligned (within ~1 s) with no continuous correction.
 *
 *   P/Q:  one frame → OCR the session clock → seek by the difference (<1 s).
 *   Race: click near a lap change → capture the lap counter 1×/s for ~10 s →
 *         batch-OCR → find the lap-increment frame → align it to the data's
 *         lap-cross. Pre-race start anchoring is on ENTER.
 *
 * Keyboard:
 *   ENTER (always available) — jump to a start instant AND resume if paused.
 *     P/Q : the next GREEN flag (session start / restart).
 *     Race: scheduled start (= formation-lap start, when the analog Rolex hand
 *           hits the hour) if the clock is within the first minute after the
 *           scheduled time; otherwise lights-out (press as the 5 lights go out).
 *           Snap is start-phase only (≤ lap 1); past that ENTER only resumes.
 *   "+"/"=" / "−" (after video sync has been used this session): nudge the data
 *     forward ~0.5 s, or pause ~0.1 s so the TV catches up.
 */

(function () {
    // OCR regions, [x0,y0,x1,y1] as fractions of the 16:9 VIDEO CONTENT rect
    // (letterbox-aware via detectContentRect). Race uses a lap-counter box; P/Q
    // uses a wide upper-left sweep (the clock's exact spot varies by broadcast).
    const REGIONS = {
        practice:   [[0.05, 0.122, 0.20, 0.178]],
        qualifying: [[0.05, 0.122, 0.20, 0.178]],
        race:       [[0.05, 0.139, 0.20, 0.172]],   // lap counter
    };
    function sessionRegions() {
        const t = ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase();
        return REGIONS[t] || REGIONS.practice;
    }
    function isRace() {
        return ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase() === 'race';
    }

    // P/Q session clock: OCR a WIDE upper-left sweep over the timing graphic and
    // regex out the clock anchored on the session badge (a tight box lands on a
    // leaderboard row). One read is enough for <1 s sync.
    const PQ_CLOCK_SWEEP = [0.0, 0.0, 0.35, 0.45];  // wide upper-left block (content-rect fractions)
    const PQ_MATCH_TOL_MS = 90000;                  // accept the MM:SS candidate within 90 s of the data clock

    const OCR_UPSCALE = 3;                          // enlarge crop for legibility
    const LUMA_THRESHOLD = 140;                     // light text → dark on white
    const WHITE_THR = 165;                          // near-white caption isolation (wide sweep)

    const LAG_COMP_S = 0.5;                         // ENTER / "+" bias the target ahead (server fetch lag)

    // Race on-demand sync: capture the lap-counter crop once a second for this
    // many frames, then batch-OCR and find when the counter ticked up.
    const RACE_BURST_FRAMES = 10;
    const RACE_BURST_INTERVAL_MS = 1000;
    const PQ_CLOCK_TRIES = 4;                       // OCR attempts for a good clock read

    // Screen-capture constraints: low fps + 1080p keep the (brief) decode light;
    // we only need one good frame (P/Q) or 1 frame/s (race).
    const CAPTURE = {
        video: { displaySurface: 'monitor', frameRate: { ideal: 5, max: 8 },
                 width: { max: 1920 }, height: { max: 1080 } },
        audio: false,
    };

    const state = {
        enabled: false,    // user has used video sync this session (gates +/− nudges)
        busy: false,       // a one-shot sync is in progress
        stream: null,
        video: null,
        content: { x0: 0, y0: 0, x1: 1, y1: 1 },   // 16:9 video rect within the frame (letterbox-aware)
        worker: null,                               // psm-7 tight single-line (clock / lap)
        textWorker: null,                           // psm-6 alphanumeric header sweep (P/Q locate)
        // race lap data, maintained by the raceLaps sub (for race-cross alignment)
        dataLap: null, dataLapAt: 0, lapIntervalMs: 90000, dataTotalLaps: null, dataCross: null,
    };

    // ── Bus helpers ──────────────────────────────────────────────────────
    // base.js declares `messageBus` as a top-level const (a global binding, NOT
    // a window property), so reference it bare.
    function bus() { return (typeof messageBus !== 'undefined') ? messageBus : null; }
    function seekTo(offsetS) {
        const b = bus();
        if (b && typeof b.send === 'function') b.send({ cmd: 'seek', offset: Math.max(0, offsetS) });
    }
    function currentOffset() {
        const b = bus();
        return (b && typeof b.getCurrentOffset === 'function') ? b.getCurrentOffset() : null;
    }
    const delay = (ms) => new Promise(r => setTimeout(r, ms));

    // ── Status light ─────────────────────────────────────────────────────
    // green = synced, yellow = working, red = couldn't read, grey = idle.
    const $ = (id) => document.getElementById(id);
    function setLight(cls) {
        const l = $('videoSyncLight');
        if (l) l.className = 'video-sync-light' + (cls ? ' ' + cls : '');
    }

    // ── Debug (localStorage videoSyncDebug=1) ────────────────────────────
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
        box.innerHTML = `<div>${label || ''}</div><img src="${canvas.toDataURL()}" style="max-width:280px;display:block">`;
    }
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
        rect(isRace() ? sessionRegions()[0] : PQ_CLOCK_SWEEP, 'lime');  // OCR region
        box.innerHTML = '<div>frame · cyan=content green=clock/lap</div>';
        box.appendChild(c);
    }

    // ── Clock parsing ────────────────────────────────────────────────────
    function parseClock(str) {
        if (!str) return null;
        const m = String(str).match(/(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.(\d))?/);
        if (!m) return null;
        const h = m[1] ? parseInt(m[1], 10) : 0;
        const min = parseInt(m[2], 10), sec = parseInt(m[3], 10);
        const tenths = m[4] ? parseInt(m[4], 10) : 0;
        if (min > 59 || sec > 59) return null;
        return (((h * 60 + min) * 60 + sec) * 10 + tenths) * 100;
    }
    // The data-side session clock the TV also shows (#sessionClock, kept live by header.js).
    function dataClockMs() {
        const el = $('sessionClock');
        return el ? parseClock(el.textContent) : null;
    }

    // ── OCR workers ──────────────────────────────────────────────────────
    async function ensureWorker() {
        if (state.worker) return state.worker;
        if (typeof Tesseract === 'undefined') { setLight('error'); return null; }
        const worker = await Tesseract.createWorker('eng');
        await worker.setParameters({
            tessedit_char_whitelist: '0123456789:./',  // clock MM:SS and lap n/total
            tessedit_pageseg_mode: '7',                 // single text line
            debug_file: '/dev/null',
        });
        state.worker = worker;
        return worker;
    }
    async function ensureTextWorker() {
        if (state.textWorker) return state.textWorker;
        if (typeof Tesseract === 'undefined') return null;
        const worker = await Tesseract.createWorker('eng');
        await worker.setParameters({
            tessedit_char_whitelist: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:./ ',
            tessedit_pageseg_mode: '6',                 // uniform block of text (multi-line)
            debug_file: '/dev/null',
        });
        state.textWorker = worker;
        return worker;
    }

    function pqBadgeRx() {
        const t = ((window.SESSION_CONFIG || {}).sessionType || '').toLowerCase();
        return t === 'qualifying'
            ? /QUAL|SHOOT|\bS?Q\s?[123]\b/i
            : /PRACT|\bFP\s?[123]\b/i;
    }
    // Every H:MM:SS / MM:SS in the OCR text, in ms (session clocks run up to ~2h).
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
    // Pre-session countdown-to-start. Both broadcasters place the clock adjacent
    // to a caption (ServusTV "STARTS IN" above; ORF "bis zum Start" below), on a
    // DIFFERENT line from the session badge — so the badge-line match misses it.
    // Take the mm:ss on/next to the caption line; else, if the badge is showing
    // and there's exactly one clock in the block, that lone clock is the
    // countdown (a running session clock would have matched the badge line).
    const COUNTDOWN_RX = /STARTS?\s*IN|BIS\s*ZUM\s*START|UNTIL\s*(?:THE\s*)?START/i;
    function countdownFromLines(lines, badgePresent) {
        const pi = lines.findIndex(l => COUNTDOWN_RX.test(l));
        if (pi >= 0) {
            for (const i of [pi, pi + 1, pi - 1, pi + 2]) {
                if (i < 0 || i >= lines.length) continue;
                const c = timeCandidates(lines[i]);
                if (c.length) return c[0];
            }
            const any = timeCandidates(lines.join(' '));
            if (any.length) return any[0];
        }
        const all = timeCandidates(lines.join(' '));
        if (badgePresent && all.length === 1) return all[0];
        return null;
    }
    function parseLap(str) {
        if (!str) return null;
        let m = String(str).match(/(\d{1,2})\s*\/\s*(\d{1,3})/);   // "6/66" → leader 6
        if (m) return parseInt(m[1], 10);
        m = String(str).match(/\b(\d{1,2})\b/);                    // fallback: first 1–2 digits
        return m ? parseInt(m[1], 10) : null;
    }

    // ── Frame capture + crop ─────────────────────────────────────────────
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
        if (!region || !v || !v.videoWidth) return null;
        const up = (opts && opts.upscale) || OCR_UPSCALE;
        const mode = (opts && opts.mode) || 'otsu';
        const c = state.content;
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
            for (let i = 0; i < d.length; i += 4) {
                const mn = Math.min(d[i], d[i + 1], d[i + 2]);
                const px = mn > WHITE_THR ? 0 : 255;
                d[i] = d[i + 1] = d[i + 2] = px; d[i + 3] = 255;
            }
        } else {
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
    // cropToCanvas reuses one backing canvas — clone when we must keep a frame.
    function cloneCanvas(src) {
        const c = document.createElement('canvas');
        c.width = src.width; c.height = src.height;
        c.getContext('2d').drawImage(src, 0, 0);
        return c;
    }

    function collectWords(data) {
        const out = [];
        const push = (w) => { if (w && w.text && w.bbox) out.push({ text: String(w.text).trim(), bbox: w.bbox }); };
        if (Array.isArray(data.words)) data.words.forEach(push);
        if (Array.isArray(data.blocks)) data.blocks.forEach(b =>
            (b.paragraphs || []).forEach(p => (p.lines || []).forEach(l => (l.words || []).forEach(push))));
        if (Array.isArray(data.lines)) data.lines.forEach(l => (l.words || []).forEach(push));
        return out;
    }

    // OCR the wide P/Q sweep, anchored on the session badge, → { ms, at } (or null).
    // `at` is the frame-capture instant (performance.now), so the caller can add
    // back the OCR elapsed time — the TV clock keeps counting down while OCR runs,
    // and without this the seek lands `ocr_duration` behind the live picture.
    async function readSessionClock() {
        const at = performance.now();
        const sweep = cropToCanvas(PQ_CLOCK_SWEEP, { upscale: 2 });
        const tw = await ensureTextWorker();
        if (!sweep || !tw) return null;
        showDebugCrop(sweep, 'clock sweep');
        let data;
        try { ({ data } = await tw.recognize(sweep)); } catch (e) { return null; }
        const badgeRe = pqBadgeRx();
        const lines = (data.text || '').split('\n').map(s => s.trim());
        const badgeLine = lines.find(ln => badgeRe.test(ln) && timeCandidates(ln).length);
        if (badgeLine) return { ms: timeCandidates(badgeLine)[0], at };
        // Pre-session countdown ("STARTS IN mm:ss" / "mm:ss bis zum Start"): the
        // clock is on its own line, so the badge-line match above misses it.
        const cdMs = countdownFromLines(lines, lines.some(ln => badgeRe.test(ln)));
        if (cdMs != null) return { ms: cdMs, at, countdown: true };
        // No badge → nearest-data across all time-like reads (filters lap times/gaps).
        const all = timeCandidates(data.text || ''), dRef = dataClockMs();
        if (all.length && dRef != null) {
            let b = null, bd = Infinity;
            for (const c of all) { const dd = Math.abs(c - dRef); if (dd < bd) { bd = dd; b = c; } }
            return bd <= PQ_MATCH_TOL_MS ? { ms: b, at } : null;
        }
        return null;
    }

    // ── On-demand sync ───────────────────────────────────────────────────
    function releaseStream() {
        if (state.stream) { try { state.stream.getTracks().forEach(t => t.stop()); } catch (e) {} state.stream = null; }
        state.video = null;
    }
    async function waitForFrame(video, timeoutMs) {
        const t0 = performance.now();
        while (!video.videoWidth && performance.now() - t0 < timeoutMs) await delay(50);
    }

    // Button handler: run one sync, then release the capture (no ongoing cost).
    async function runSync() {
        if (state.busy) return;
        releaseStream();
        state.busy = true; state.enabled = true;
        setLight('arming');
        try {
            state.stream = await navigator.mediaDevices.getDisplayMedia(CAPTURE);
        } catch (e) {
            setLight('');                      // share cancelled / denied
            state.busy = false; return;
        }
        try {
            state.video = document.createElement('video');
            state.video.muted = true;
            state.video.srcObject = state.stream;
            await state.video.play().catch(() => {});
            await waitForFrame(state.video, 3000);
            state.content = detectContentRect();
            showDebugFrame();
            const ok = isRace() ? await syncRaceOnce() : await syncClockOnce();
            setLight(ok ? 'ok' : 'error');
        } catch (e) {
            dbg('sync error', e);
            setLight('error');
        } finally {
            releaseStream();                   // tear down the capture — zero idle cost
            state.busy = false;
        }
    }

    // P/Q: read the clock once, seek the data so it matches. Both count DOWN, so
    // to show tvMs remaining we move by (dMs − tvMs): + → back, − → forward.
    async function syncClockOnce() {
        let read = null;
        for (let i = 0; i < PQ_CLOCK_TRIES && read == null; i++) {
            read = await readSessionClock();
            if (read == null) await delay(300);
        }
        const now = performance.now();
        const cur = currentOffset();
        if (read == null || cur == null) {
            dbg('clock sync: incomplete read', read, cur);
            return false;
        }
        // The TV clock counted down by `elapsedS` while OCR ran; add it back so we
        // align to the LIVE picture, not the (now-stale) captured frame.
        const elapsedS = (now - read.at) / 1000;

        // Pre-session countdown: mm:ss is time-until-start. There is no matching
        // data-side clock (the data shows session duration, not a countdown), so
        // anchor to the scheduled session start instead: place the data the same
        // distance before it. (offset 0 = SessionInfo; scheduledStartMs from it.)
        if (read.countdown) {
            const b = bus();
            if (scheduledStartMs == null || !b || !b.startTime) {
                dbg('countdown sync: no scheduled start'); return false;
            }
            const startOffS = (scheduledStartMs - b.startTime.getTime()) / 1000;
            const tvRemainS = read.ms / 1000 - elapsedS;
            const target = startOffS - tvRemainS;
            dbg('countdown sync: start@', startOffS.toFixed(1), 'tvRemain',
                tvRemainS.toFixed(1), '→', target.toFixed(1));
            if (target >= 0) seekTo(target);
            return true;
        }

        const dMs = dataClockMs();
        if (dMs == null) { dbg('clock sync: no data clock'); return false; }
        const tvMs = read.ms;
        const deltaS = (dMs - tvMs) / 1000 + elapsedS;
        dbg('clock sync: tv', (tvMs / 1000).toFixed(1), 'data', (dMs / 1000).toFixed(1),
            'ocr elapsed', elapsedS.toFixed(2), 'Δ', deltaS.toFixed(2));
        if (Math.abs(deltaS) >= 0.5) { seekTo(cur + deltaS); dbg('clock sync: seek', (cur + deltaS).toFixed(1)); }
        return true;
    }

    // Race: capture the lap-counter crop 1×/s for ~10 s, then batch-OCR and find
    // the frame where the counter ticked up — that's the TV lap-cross. Align it to
    // the data's lap-cross for the same lap. (Click near a lap change.)
    async function syncRaceOnce() {
        const w = await ensureWorker();
        if (!w) return false;
        const frames = [];
        for (let i = 0; i < RACE_BURST_FRAMES; i++) {
            const crop = cropToCanvas(sessionRegions()[0]);
            if (crop) { showDebugCrop(crop, `lap frame ${i + 1}/${RACE_BURST_FRAMES}`); frames.push({ img: cloneCanvas(crop), at: performance.now() }); }
            if (i < RACE_BURST_FRAMES - 1) await delay(RACE_BURST_INTERVAL_MS);
        }
        const reads = [];
        for (const f of frames) {
            let lap = null;
            try { const { data } = await w.recognize(f.img); lap = parseLap(data.text); } catch (e) {}
            reads.push({ lap, at: f.at });
        }
        dbg('race frames →', reads.map(r => r.lap));
        // First lap increment within the window = the TV lap-cross.
        let crossAt = null, crossLap = null;
        for (let i = 1; i < reads.length; i++) {
            const a = reads[i - 1].lap, b = reads[i].lap;
            if (a != null && b != null && b === a + 1) { crossAt = reads[i].at; crossLap = b; break; }
        }
        if (crossAt == null) { dbg('race sync: no lap increment in the capture window — click nearer a lap change'); return false; }
        const dCross = (state.dataCross && state.dataCross.lap === crossLap) ? state.dataCross : null;
        const cur = currentOffset();
        if (!dCross || cur == null) { dbg('race sync: no matching data lap-cross for lap', crossLap); return false; }
        const offsetS = (dCross.at - crossAt) / 1000;   // +: data crossed later ⇒ behind ⇒ seek forward
        seekTo(cur + offsetS);
        dbg('race sync: lap', crossLap, 'Δ', offsetS.toFixed(2), '→ seek', (cur + offsetS).toFixed(1));
        return true;
    }

    // ── Data leader lap (maintained for the race-cross alignment) ────────
    messageBus.on('raceLaps', (data) => {
        if (!data || typeof data.currentLap !== 'number') return;
        if (data.totalLaps) state.dataTotalLaps = data.totalLaps;
        const L = data.currentLap;
        if (state.dataLap != null && L !== state.dataLap) {
            const now = performance.now();
            if (state.dataLapAt) {
                const iv = now - state.dataLapAt;
                if (iv > 20000 && iv < 300000) state.lapIntervalMs = 0.5 * state.lapIntervalMs + 0.5 * iv;
            }
            state.dataLapAt = now;
            state.dataCross = { lap: L, at: now };
        }
        state.dataLap = L;
    });

    // ── Start / restart anchors (ENTER) ──────────────────────────────────
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
    });
    function seekAhead(offsetS) { seekToOffset(Math.max(0, offsetS + LAG_COMP_S)); }

    // ENTER — jump to the next start / restart.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.isComposing) return;
        if (typeof seekToOffset !== 'function') return;
        const b = bus();
        if (!b || !b.startTime || typeof b.getCurrentOffset !== 'function') return;
        const cur = b.getCurrentOffset();
        if (isRace()) {
            // Snap to one of two unambiguously-identifiable instants, split at
            // scheduled-start + 60 s:
            //   • clock BEFORE sched+60s → scheduled start (= formation-lap
            //     start; the analog Rolex hand hacks onto the hour). The full
            //     minute of leeway lets the user hit ENTER on that exact moment
            //     even if the data/audio feed momentarily leads the TV.
            //   • clock AFTER  sched+60s → lights-out (no fixed time — the user
            //     presses ENTER when the 5 lights go out). Formation runs ~2-4
            //     min, so the two instants never overlap the 60 s boundary.
            // Snap is start-phase only (≤ lap 1) so a mid-race ENTER (e.g. to
            // resume after pausing to let the TV catch up) does NOT yank back to
            // lights-out. ENTER also RESUMES playback whenever it is paused.
            const schedS = scheduledStartMs != null ? (scheduledStartMs - b.startTime.getTime()) / 1000 : null;
            const greenS = raceStartMs != null ? raceStartMs / 1000 : null;
            const startPhase = (state.dataLap == null || state.dataLap < 2);
            let handled = false;
            if (startPhase) {
                if (schedS != null && cur < schedS + 60) { seekAhead(schedS); handled = true; }   // → scheduled start
                else if (greenS != null) { seekAhead(greenS); handled = true; }                    // → lights-out
            }
            if (b.isPlaying === false && typeof b.send === 'function') { b.send({ cmd: 'play' }); handled = true; }
            if (handled) e.preventDefault();
            return;
        }
        const next = greenOffsetsMs.find(o => o / 1000 > cur + 1);
        if (next != null) { e.preventDefault(); seekAhead(next / 1000); }
    });

    // Manual fine nudges (once video sync has been used this session):
    //   "+" / "=" : TV ahead → skip the data forward ~0.5 s.
    //   "−"       : TV behind → pause ~0.1 s and resume so the TV catches up.
    document.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.isComposing) return;
        if (!state.enabled) return;
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

    window.toggleVideoSync = runSync;
})();
