/**
 * Telemetry Tile
 *
 * Displays speed/throttle/brake/gear/rpm traces per driver.
 *
 * Live data: combines raw CarData.z (channels) with position (distPct) per driver.
 * Completed laps: queries lapTelemetry:{num}:{lap} from server on demand.
 *
 * Interaction: clicking a lap in the run plan toggles it in the selection.
 * Selected laps are displayed as overlaid traces.
 *
 * Channels: speed (default), rpm, gear, throttle+brake.
 * X-axis: track distance % (0-100).
 */

(function() {
    const CHANNELS = {
        speed:         { idx: 1, label: 'Speed (km/h)', min: 0, max: 350 },
        rpm:           { idx: 2, label: 'RPM',          min: 5500, max: 13500 },
        gear:          { idx: 3, label: 'Gear',          min: 0, max: 9 },
        throttleBrake: { idx: -1, label: 'Thr/Brk',     min: 0, max: 100 },
    };

    const state = {
        drivers: {},          // num -> {tla, color}
        activeChannel: 'speed',
        mode: 'live',         // 'live'|'last'|'best'|'selection'
        // Independent lap sets per view (I14). Live uses liveSamples; Last/Best
        // keep ≤1 lap per driver (replaced on a newer/better lap); Selection is
        // the user's manual picks — only toggleLap mutates it, it survives view
        // switches and seeks. All keyed "num:lap".
        lastLaps: {},
        bestLaps: {},
        selectionLaps: {},
        // Pending telemetry requests so a telemetryLap response lands in the
        // bucket that asked for it; unsolicited (replay-streamed) laps are
        // ignored → nothing auto-adds to a view.
        pendingSelection: new Set(),   // "num:lap"
        pendingLast: new Set(),        // num
        pendingBest: new Set(),        // num
        lastSeenLastLap: {},           // num -> last lastLap.lap (Last auto-refresh)
        liveSamples: {},      // num -> [[distPct, spd, rpm, gear, thr, brk], ...]
        liveLap: {},          // num -> current lap of the live trace (from liveTelemetry)
        driverStatus: {},     // num -> "PIT"|"OUT"|"TRACK"|...
        standingsOrder: [],   // [num] in standings position order (card 72: Last/Best ordering)
        lapTimes: {},         // num -> {lapNum -> "1:23.456"}
        lapCls: {},           // num -> {lapNum -> type}
        bestLapNum: {},       // num -> driver's fastest lap number (driverLaps.bestLap) → purple pill
        lapSegments: {},      // num -> {lapNum -> qualSegment} (unused: no source topic now)
        lapNoData: {},        // num -> Set(lapNum) — laps that came in empty
        telemetryLaps: {},    // num -> Set(laps that have telemetry on disk)
        hiddenDrivers: new Set(),
        canvas: null,
        ctx: null,
        renderPending: false,
        lapListScrollLeft: 0,  // shared horizontal scroll across all driver lap-list strips
        // Race-only: hide all lap pills until the race has actually
        // started (= sessionStatus → "Started"). Pre-race emits like
        // OUT/PIT for an installation lap should not render selectable
        // pills, since those pre-race "laps" aren't part of the race.
        raceStarted: false,
        corners: [],           // [{number, pct}] from trackGeometry
        // X-axis zoom — visible window over lap distance (% units).
        // [0, 100] = full lap; mouse wheel zooms (anchored to cursor),
        // click-drag pans, double-click resets. drawX() projects sample
        // pct → canvas px honouring this window.
        xMin: 0,
        xMax: 100,
    };

    // Project a lap-distance percentage (0..100) → canvas X within the
    // current zoom window. Returns NaN if pct lies outside the window —
    // callers either clip or skip such samples.
    function pctToX(pct, leftMargin, plotW) {
        const span = state.xMax - state.xMin;
        if (span <= 0) return leftMargin;
        return leftMargin + ((pct - state.xMin) / span) * plotW;
    }
    function xToPct(xPx, leftMargin, plotW) {
        const span = state.xMax - state.xMin;
        return state.xMin + ((xPx - leftMargin) / plotW) * span;
    }

    // The lap set overlaid on the chart for the active view (I14).
    function currentLaps() {
        if (state.mode === 'last') return state.lastLaps;
        if (state.mode === 'best') return state.bestLaps;
        if (state.mode === 'selection') return state.selectionLaps;
        return {};   // live → no overlaid completed laps
    }

    function scheduleRender() {
        if (state.renderPending) return;
        state.renderPending = true;
        requestAnimationFrame(() => {
            state.renderPending = false;
            renderChart();
        });
    }

    /**
     * Return drivers grouped by team, teams ordered by lowest driver number.
     * Within each team, lowest number first. Result: [{num, teamOrder}] where
     * teamOrder is 0 for the first team-mate and 1 for the second.
     */
    function getSortedDrivers() {
        const teams = {};
        for (const [num, d] of Object.entries(state.drivers)) {
            const key = d.teamName || d.color || num;
            (teams[key] || (teams[key] = [])).push(num);
        }
        const groups = Object.entries(teams).map(([key, nums]) => {
            nums.sort((a, b) => parseInt(a) - parseInt(b));
            return { key, nums };
        });
        // Teams ordered by lowest car number. (Constructors'-championship
        // ordering was dropped — the FIA feed doesn't always send team names,
        // so there's no reliable team→rank mapping; car number is enough.)
        groups.sort((a, b) => parseInt(a.nums[0]) - parseInt(b.nums[0]));
        const result = [];
        for (const g of groups) {
            g.nums.forEach((num, i) => result.push({ num, teamOrder: i }));
        }
        return result;
    }

    function getContrastColor(hex) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#000' : '#fff';
    }

    // =========================================================================
    // Init
    // =========================================================================

    // Mouse-wheel zoom (anchored to cursor X), click-drag pan, double-
    // click reset. Plot left margin + plot width come from the same
    // values used by renderSingle (margin.left=45, right=10).
    function setupZoomInteractions() {
        if (!state.canvas) return;
        const LEFT = 45, RIGHT = 10;
        const plotW = () => state.canvas.clientWidth - LEFT - RIGHT;

        state.canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const rect = state.canvas.getBoundingClientRect();
            const cursorX = e.clientX - rect.left;
            const pw = plotW();
            if (pw <= 0) return;
            const cursorPct = xToPct(cursorX, LEFT, pw);
            const scale = e.deltaY > 0 ? 1.25 : 0.8;     // out / in
            let newSpan = (state.xMax - state.xMin) * scale;
            newSpan = Math.max(2, Math.min(100, newSpan));   // 2 % min span, 100 % max
            // Anchor the zoom around the cursor's lap-distance position.
            let newMin = cursorPct - ((cursorX - LEFT) / pw) * newSpan;
            let newMax = newMin + newSpan;
            if (newMin < 0)   { newMax -= newMin; newMin = 0; }
            if (newMax > 100) { newMin -= (newMax - 100); newMax = 100; }
            state.xMin = Math.max(0, newMin);
            state.xMax = Math.min(100, newMax);
            scheduleRender();
            renderCornerLabels();
        }, { passive: false });

        let dragging = false, dragStartX = 0, dragStartMin = 0, dragStartMax = 0;
        state.canvas.addEventListener('mousedown', (e) => {
            // Only pan when zoomed in (full view has nowhere to pan).
            if (state.xMax - state.xMin >= 99.9) return;
            dragging = true;
            dragStartX = e.clientX;
            dragStartMin = state.xMin;
            dragStartMax = state.xMax;
            state.canvas.style.cursor = 'grabbing';
        });
        window.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            const pw = plotW();
            if (pw <= 0) return;
            const span = dragStartMax - dragStartMin;
            const deltaPct = -((e.clientX - dragStartX) / pw) * span;
            let newMin = dragStartMin + deltaPct;
            let newMax = dragStartMax + deltaPct;
            if (newMin < 0)   { newMax -= newMin; newMin = 0; }
            if (newMax > 100) { newMin -= (newMax - 100); newMax = 100; }
            state.xMin = newMin;
            state.xMax = newMax;
            scheduleRender();
            renderCornerLabels();
        });
        window.addEventListener('mouseup', () => {
            dragging = false;
            if (state.canvas) state.canvas.style.cursor = '';
        });
        state.canvas.addEventListener('dblclick', () => {
            state.xMin = 0;
            state.xMax = 100;
            scheduleRender();
            renderCornerLabels();
        });
    }

    function init() {
        state.canvas = document.getElementById('telemetryCanvas');
        if (!state.canvas) return;
        state.ctx = state.canvas.getContext('2d');

        document.querySelectorAll('.telemetry-channel').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.telemetry-channel').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.activeChannel = btn.dataset.channel;
                resizeCanvas();
                renderChart();
            });
        });

        document.querySelectorAll('.telemetry-mode').forEach(btn => {
            btn.addEventListener('click', () => setMode(btn.dataset.mode));
        });

        // ALL DRIVERS toggle — event-delegated so it works regardless of
        // when the button is in the DOM (header or re-rendered driver
        // panel). Matches anything with data-action="all".
        document.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-action="all"]');
            if (!btn) return;
            if (!btn.classList.contains('telemetry-driver-toggle-all')) return;
            const willShowAll = state.hiddenDrivers.size > 0;
            if (state.hiddenDrivers.size === 0) {
                state.hiddenDrivers = new Set(Object.keys(state.drivers));
            } else {
                state.hiddenDrivers.clear();
            }
            if (willShowAll && (state.mode === 'last' || state.mode === 'best')) {
                fetchLapsForVisibleDrivers(state.mode);
            }
            renderDriverSelector();
            scheduleRender();
        });

        setupZoomInteractions();

        resizeCanvas();
        renderChart();
        renderCornerLabels();
        window.addEventListener('resize', () => {
            resizeCanvas();
            renderChart();
            renderCornerLabels();
        });
    }

    function setMode(mode) {
        state.mode = mode;
        document.querySelectorAll('.telemetry-mode').forEach(b => {
            b.classList.toggle('active', b.dataset.mode === mode);
        });
        // Last/Best are recomputed snapshots — cleared and refetched on entry.
        // Selection is the user's set and is never cleared by a view switch.
        if (mode === 'last') { state.lastLaps = {}; fetchLapsForVisibleDrivers('last'); }
        else if (mode === 'best') { state.bestLaps = {}; fetchLapsForVisibleDrivers('best'); }
        updateDriverBar();
        scheduleRender();
    }

    function fetchLapsForVisibleDrivers(mode) {
        const cmd = mode === 'last' ? 'getLastLapTelemetry' : 'getBestLapTelemetry';
        const pend = mode === 'last' ? state.pendingLast : state.pendingBest;
        for (const num of Object.keys(state.drivers)) {
            if (state.hiddenDrivers.has(num)) continue;
            pend.add(num);
            messageBus.send({ cmd, driver: num });
        }
    }

    function countDriversToStack() {
        // Drivers contributing to the stacked throttle/brake view: visible + have data
        let n = 0;
        for (const num of Object.keys(state.drivers)) {
            if (state.hiddenDrivers.has(num)) continue;
            const hasLive = state.mode === 'live' && state.liveSamples[num] && state.liveSamples[num].length;
            const hasSelected = state.mode !== 'live' &&
                Object.values(currentLaps()).some(l => l.driver === num);
            if (hasLive || hasSelected) n++;
        }
        return n;
    }

    function resizeCanvas() {
        if (!state.canvas) return;
        const wrapper = state.canvas.parentElement;        // .telemetry-chart
        const scroller = wrapper && wrapper.parentElement; // .telemetry-charts
        if (!scroller) return;
        const dpr = window.devicePixelRatio || 1;
        const cssW = scroller.clientWidth;
        let cssH = scroller.clientHeight;

        if (state.activeChannel === 'throttleBrake') {
            const n = countDriversToStack();
            const bandUnit = cssH * 0.22;
            if (n > 4) cssH = Math.max(cssH, Math.ceil(n * bandUnit));
        }

        const targetW = Math.round(cssW * dpr);
        const targetH = Math.round(cssH * dpr);
        if (state.canvas.width !== targetW || state.canvas.height !== targetH) {
            state.canvas.width = targetW;
            state.canvas.height = targetH;
            state.canvas.style.width = cssW + 'px';
            state.canvas.style.height = cssH + 'px';
            if (state.ctx) state.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
    }

    // =========================================================================
    // Lap Selection (from run plan clicks)
    // =========================================================================

    function toggleLap(driverNum, lapIndex) {
        // A manual pick goes to the Selection set (independent of Last/Best).
        // Switch to the Selection view so the click is visible; the set then
        // persists across later view switches and seeks.
        if (state.mode !== 'selection') setMode('selection');
        const key = `${driverNum}:${lapIndex}`;
        if (state.selectionLaps[key]) {
            delete state.selectionLaps[key];
            renderChart();
            updateDriverBar();
        } else {
            state.pendingSelection.add(key);
            messageBus.send({ cmd: 'getLapTelemetry', driver: driverNum, lap: lapIndex });
        }
    }

    window.telemetryToggleLap = toggleLap;

    function handleLapTelemetry(topic, data) {
        const parts = topic.split(':');
        if (parts.length < 3) return;
        const driverNum = parts[1];
        const lap = parseInt(parts[2]);
        const key = `${driverNum}:${lap}`;

        const driver = state.drivers[driverNum];
        const color = driver ? driver.color : (TEAM_COLORS[driverNum] || DEFAULT_CAR_COLOR);

        // Track laps that arrived with no telemetry samples (empty array
        // due to position-data outage covering 2+ S/F crossings).
        if (Array.isArray(data) && data.length === 0) {
            if (!state.lapNoData[driverNum]) state.lapNoData[driverNum] = new Set();
            state.lapNoData[driverNum].add(lap);
        }

        // F1 LapTime (from TimingData) is the source of truth for the
        // legend, not the computed telemetry duration.
        const lapTime = (state.lapTimes[driverNum] || {})[lap] || '';
        const obj = {
            driver: driverNum,
            lap: lap,
            samples: data,
            color: color,
            tla: driver ? driver.tla : driverNum,
            lapTime: lapTime,
        };

        // Route to whichever view requested it. A lap with no pending request
        // (e.g. replay-streamed on lap close) is ignored → nothing auto-adds.
        let used = false;
        if (state.pendingSelection.has(key)) {
            state.pendingSelection.delete(key);
            state.selectionLaps[key] = obj;
            used = true;
        }
        if (state.pendingLast.has(driverNum)) {
            state.pendingLast.delete(driverNum);
            for (const k of Object.keys(state.lastLaps)) {
                if (state.lastLaps[k].driver === driverNum) delete state.lastLaps[k];
            }
            state.lastLaps[key] = obj;       // replace this driver's Last lap
            used = true;
        }
        if (state.pendingBest.has(driverNum)) {
            state.pendingBest.delete(driverNum);
            for (const k of Object.keys(state.bestLaps)) {
                if (state.bestLaps[k].driver === driverNum) delete state.bestLaps[k];
            }
            state.bestLaps[key] = obj;       // replace this driver's Best lap
            used = true;
        }
        if (!used) return;

        renderChart();
        updateDriverBar();
    }

    // =========================================================================
    // Live Telemetry
    // =========================================================================

    // One server-decoded sample: {dp, speed, rpm, gear, throttle, brake,
    // ts, lap, lapElapsedMs}. dp is the track distance %. A change in `lap`
    // marks an S/F crossing → start a fresh live-lap trace. Samples with a
    // null dp (position outage) are skipped.
    function handleLiveTelemetry(num, data) {
        if (!data || typeof data !== 'object') return;
        if (data.dp == null) return;

        // New lap → reset the live trace so it shows the current lap only.
        if (state.liveLap[num] !== undefined && data.lap !== state.liveLap[num]) {
            state.liveSamples[num] = [];
        }
        state.liveLap[num] = data.lap;

        const sample = [
            data.dp,
            data.speed || 0,
            data.rpm || 0,
            data.gear || 0,
            data.throttle || 0,
            data.brake || 0,
        ];
        if (!state.liveSamples[num]) state.liveSamples[num] = [];
        state.liveSamples[num].push(sample);
        if (state.liveSamples[num].length > 500) {
            state.liveSamples[num] = state.liveSamples[num].slice(-400);
        }
        scheduleRender();
    }

    function handleDriverStatus(num, status) {
        state.driverStatus[num] = status;
        // Drop the live trace when the car goes off track (RET/STOP) so a
        // dead car's trace doesn't linger (card 54). PIT/OUT no longer clear
        // here — a race shows those laps, and the per-lap reset in
        // handleLiveTelemetry keeps flying laps clean.
        if (status === 'RET' || status === 'STOP') {
            state.liveSamples[num] = [];
        }
        scheduleRender();
    }

    // =========================================================================
    // Rendering
    // =========================================================================

    function renderChart() {
        resizeCanvas();
        const ctx = state.ctx;
        const canvas = state.canvas;
        if (!ctx || !canvas) return;

        const dpr = window.devicePixelRatio || 1;
        const w = canvas.width / dpr;
        const h = canvas.height / dpr;

        ctx.clearRect(0, 0, w, h);

        const channel = state.activeChannel;
        if (channel === 'throttleBrake') {
            renderStackedThrottleBrake(ctx, w, h);
        } else {
            renderSingle(ctx, w, h, channel);
        }
    }

    // Whether a driver's LIVE trace is suppressed — by driverStatus ONLY,
    // never lap classification (card 54):
    //   • RET / STOP (off track): suppressed in every session.
    //   • P/Q additionally suppress OUT (out-lap) and PIT (in pit lane).
    //     A race shows OUT/PIT so out- and in-laps can be followed.
    function liveTraceSuppressed(num) {
        const s = state.driverStatus[num];
        if (s === 'RET' || s === 'STOP') return true;
        const sType = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
        const isRaceLike = (sType === 'race' || sType === 'sprint');
        if (!isRaceLike && (s === 'OUT' || s === 'PIT')) return true;
        return false;
    }

    function renderSingle(ctx, w, h, channel) {
        const margin = { top: 10, right: 10, bottom: 20, left: 45 };
        const plotW = w - margin.left - margin.right;
        const plotH = h - margin.top - margin.bottom;
        if (plotW <= 0 || plotH <= 0) return;

        const chInfo = CHANNELS[channel];
        const yMin = chInfo.min;
        const yMax = chInfo.max;

        drawGrid(ctx, margin, plotW, plotH, yMin, yMax, h);
        drawYellowSectors(ctx, margin, plotW, plotH);

        const teamOrder = {};
        for (const { num, teamOrder: to } of getSortedDrivers()) teamOrder[num] = to;

        if (state.mode !== 'live') {
            for (const lap of Object.values(currentLaps())) {
                if (state.hiddenDrivers.has(lap.driver)) continue;
                if (!lap.samples || !lap.samples.length) continue;
                // Debug mode: render every selected lap regardless of class
                // so OUT/COOL/ABORT traces can be inspected.
                const dashed = teamOrder[lap.driver] === 1;
                drawTrace(ctx, lap.samples, lap.color, channel, margin, plotW, plotH, yMin, yMax, dashed);
                drawMarker(ctx, lap.samples[lap.samples.length - 1], lap.color, lap.tla,
                    channel, margin, plotW, plotH, yMin, yMax);
            }
        } else {
            for (const [num, samples] of Object.entries(state.liveSamples)) {
                if (state.hiddenDrivers.has(num)) continue;
                if (!samples.length) continue;
                // Live-trace visibility is driverStatus-only now (card 54):
                // RET/STOP everywhere, plus OUT/PIT in P/Q. No lap-class gate,
                // no cool-down fade.
                if (liveTraceSuppressed(num)) continue;
                const driver = state.drivers[num];
                const color = driver ? driver.color : DEFAULT_CAR_COLOR;
                const tla = driver ? driver.tla : num;
                const dashed = teamOrder[num] === 1;
                drawTrace(ctx, samples, color, channel, margin, plotW, plotH, yMin, yMax, dashed);
                drawMarker(ctx, samples[samples.length - 1], color, tla,
                    channel, margin, plotW, plotH, yMin, yMax);
            }
        }
    }

    function drawGrid(ctx, margin, plotW, plotH, yMin, yMax, h) {
        ctx.strokeStyle = 'rgba(255,255,255,0.08)';
        ctx.lineWidth = 0.5;
        // X-grid stride adapts to the zoom span — finer ticks when zoomed in.
        const xSpan = state.xMax - state.xMin;
        const stride = xSpan > 50 ? 10 : xSpan > 20 ? 5 : xSpan > 10 ? 2 : 1;
        const startTick = Math.ceil(state.xMin / stride) * stride;
        for (let pct = startTick; pct <= state.xMax + 1e-6; pct += stride) {
            const x = pctToX(pct, margin.left, plotW);
            ctx.beginPath();
            ctx.moveTo(x, margin.top);
            ctx.lineTo(x, margin.top + plotH);
            ctx.stroke();
        }
        const yTicks = 5;
        for (let i = 0; i <= yTicks; i++) {
            const y = margin.top + (i / yTicks) * plotH;
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(margin.left + plotW, y);
            ctx.stroke();
        }
        ctx.fillStyle = '#666';
        ctx.font = '10px Monaco, Consolas, monospace';
        ctx.textAlign = 'right';
        for (let i = 0; i <= yTicks; i++) {
            const val = yMax - (i / yTicks) * (yMax - yMin);
            const y = margin.top + (i / yTicks) * plotH;
            ctx.fillText(Math.round(val), margin.left - 4, y + 3);
        }
        ctx.textAlign = 'center';
        for (let pct = startTick; pct <= state.xMax + 1e-6; pct += stride) {
            const x = pctToX(pct, margin.left, plotW);
            const label = stride < 1 ? pct.toFixed(1) : String(Math.round(pct));
            ctx.fillText(`${label}%`, x, h - 4);
        }

        drawCornerMarkers(ctx, margin.left, margin.top, plotW, plotH);
    }

    // Yellow dotted vertical line at each corner's distPct, spanning the
    // full plot height. Corner numbers are drawn separately in the HTML
    // x-axis strip (renderCornerLabels) for crisp text and easy resizing.
    function drawCornerMarkers(ctx, left, top, plotW, plotH) {
        if (!state.corners || state.corners.length === 0) return;
        ctx.save();
        ctx.strokeStyle = 'rgba(255, 213, 0, 0.45)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        for (const c of state.corners) {
            const pct = Number(c.pct);
            if (!isFinite(pct) || pct < state.xMin || pct > state.xMax) continue;
            const x = pctToX(pct, left, plotW);
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + plotH);
            ctx.stroke();
        }
        ctx.restore();
    }

    function renderStackedThrottleBrake(ctx, w, h) {
        const teamOrder = {};
        for (const { num, teamOrder: to } of getSortedDrivers()) teamOrder[num] = to;

        // Collect drivers to render in sorted (team) order
        const rows = [];
        for (const { num } of getSortedDrivers()) {
            if (state.hiddenDrivers.has(num)) continue;
            const driver = state.drivers[num] || {};
            const color = driver.color || DEFAULT_CAR_COLOR;
            const tla = driver.tla || num;
            const dashed = teamOrder[num] === 1;

            let samples = null;
            if (state.mode === 'live') {
                if (liveTraceSuppressed(num)) continue;  // RET/STOP (+ OUT/PIT in P/Q)
                samples = state.liveSamples[num];
            } else {
                const lap = Object.values(currentLaps()).find(l => l.driver === num);
                if (!lap) continue;
                // Debug mode: render every selected lap regardless of class.
                samples = lap.samples;
            }
            if (samples && samples.length) rows.push({ num, color, tla, samples, dashed });
        }
        if (!rows.length) return;

        const left = 45, right = 10, topGap = 6;
        const plotW = w - left - right;
        const bandUnit = h / Math.max(4, rows.length);
        // Half the previous trace height (was bandUnit * 0.82) so the
        // throttle/brake band is more compact within each driver row.
        const plotBandH = Math.max(10, bandUnit * 0.41);

        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            const top = topGap + i * bandUnit;
            const margin = { top, right, bottom: 0, left };

            // Band grid (bottom axis + faint horizontals at 0/50/100)
            ctx.strokeStyle = 'rgba(255,255,255,0.08)';
            ctx.lineWidth = 0.5;
            for (const p of [0, 50, 100]) {
                const y = top + (1 - p / 100) * plotBandH;
                ctx.beginPath();
                ctx.moveTo(left, y);
                ctx.lineTo(left + plotW, y);
                ctx.stroke();
            }

            // Driver TLA on the left
            ctx.fillStyle = row.color;
            ctx.font = 'bold 11px Monaco, Consolas, monospace';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.fillText(row.tla, left - 6, top + plotBandH / 2);
            ctx.textBaseline = 'alphabetic';

            drawLine(ctx, row.samples, '#00cc00', 4, margin, plotW, plotBandH, 0, 100, row.dashed);
            drawLine(ctx, row.samples, '#cc0000', 5, margin, plotW, plotBandH, 0, 100, row.dashed);
        }

        // Corner markers span the full stacked chart height.
        drawCornerMarkers(ctx, left, topGap, plotW, h - topGap);
    }

    function drawMarker(ctx, sample, color, tla, channel, margin, plotW, plotH, yMin, yMax) {
        // For throttle/brake, anchor marker to throttle value
        const valIdx = channel === 'throttleBrake' ? 4 : CHANNELS[channel].idx;
        const range = yMax - yMin || 1;
        // Skip marker if its sample falls outside the zoom window.
        if (sample[0] < state.xMin || sample[0] > state.xMax) return;
        const x = pctToX(sample[0], margin.left, plotW);
        const y = margin.top + (1 - (sample[valIdx] - yMin) / range) * plotH;

        const r = 11.25;   // +25% (card 70)
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();

        ctx.fillStyle = getContrastColor(color);
        ctx.font = 'bold 11px Monaco, Consolas, monospace';   // +2px (card 70)
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(tla, x, y);
        ctx.textBaseline = 'alphabetic';
    }

    function drawTrace(ctx, samples, color, channel, margin, plotW, plotH, yMin, yMax, dashed) {
        if (channel === 'throttleBrake') {
            drawLine(ctx, samples, '#00cc00', 4, margin, plotW, plotH, 0, 100, dashed);
            drawLine(ctx, samples, '#cc0000', 5, margin, plotW, plotH, 0, 100, dashed);
        } else {
            drawLine(ctx, samples, color, CHANNELS[channel].idx, margin, plotW, plotH, yMin, yMax, dashed);
        }
    }

    // A sample is an OUTAGE when EITHER:
    //  (a) any channel is null/undefined (= the F1 feed dropped this
    //      sample but the position feed kept emitting), or
    //  (b) all 4 main channels are zero (= the F1 feed kept emitting
    //      with zero placeholders).
    // Real grid-start standstill rarely lasts long enough to look like
    // an outage; we accept that edge case as a cosmetic blip rather
    // than complicating the test.
    function isOutageSample(s) {
        return s[1] == null || s[2] == null || s[4] == null || s[5] == null
            || (s[1] === 0 && s[2] === 0 && s[4] === 0 && s[5] === 0);
    }

    function drawLine(ctx, samples, color, valIdx, margin, plotW, plotH, yMin, yMax, dashed) {
        // Clip to the plot rect so zoomed-out portions don't bleed into
        // the y-axis label area. The clipping rect lives in its OWN path
        // so the subsequent stroke() only strokes the trace, not the
        // plot border.
        ctx.save();
        ctx.beginPath();
        ctx.rect(margin.left, margin.top, plotW, plotH);
        ctx.clip();

        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        const range = yMax - yMin || 1;
        const solidDash = dashed ? [6, 4] : [];
        const bridgeDash = [2, 3];  // thin dotted bridge across outages

        // Walk samples once, drawing solid runs of valid samples and
        // thin dotted bridges across outage gaps. ONE pass, alternating
        // pen styles via beginPath()/stroke().
        let inRun = false;
        let lastValid = null;  // {x, y} of the last valid sample drawn

        for (const s of samples) {
            const outage = isOutageSample(s);
            if (outage) {
                if (inRun) {
                    // End the solid run.
                    ctx.stroke();
                    inRun = false;
                }
                continue;
            }
            const x = pctToX(s[0], margin.left, plotW);
            const val = s[valIdx];
            const y = margin.top + (1 - (val - yMin) / range) * plotH;
            if (!inRun) {
                // Bridge across the gap if we have a previous valid point.
                if (lastValid) {
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1;
                    ctx.setLineDash(bridgeDash);
                    ctx.beginPath();
                    ctx.moveTo(lastValid.x, lastValid.y);
                    ctx.lineTo(x, y);
                    ctx.stroke();
                }
                // Start a new solid run.
                ctx.strokeStyle = color;
                ctx.lineWidth = 2.5;
                ctx.setLineDash(solidDash);
                ctx.beginPath();
                ctx.moveTo(x, y);
                inRun = true;
            } else {
                ctx.lineTo(x, y);
            }
            lastValid = { x, y };
        }
        if (inRun) ctx.stroke();
        ctx.restore();
        ctx.setLineDash([]);
    }

    // =========================================================================
    // Driver Selector (toggle each driver on/off)
    // =========================================================================

    // Throttle the lap-pill rebuild — driverLaps/driverLapClassification fire
    // for 20 cars many times a second; rebuilding the pill DOM on each floods
    // the main thread. Coalesce to once per animation frame.
    let _selectorPending = false;
    function renderDriverSelector() {
        if (_selectorPending) return;
        _selectorPending = true;
        requestAnimationFrame(() => { _selectorPending = false; renderDriverSelectorNow(); });
    }

    function renderDriverSelectorNow() {
        const el = document.getElementById('telemetryDriverSelector');
        if (!el) return;
        // Default order: team / car-number. In Last/Best, order the rows by
        // standings instead (card 72), keeping each driver's teamOrder (drives
        // the dashed second-car styling). Drivers absent from standings (not
        // yet classified) fall back to default order at the end.
        const sortedDefault = getSortedDrivers();
        const teamOrderMap = {};
        for (const { num, teamOrder } of sortedDefault) teamOrderMap[num] = teamOrder;

        let sorted;
        if ((state.mode === 'last' || state.mode === 'best') && state.standingsOrder.length) {
            const seen = new Set();
            sorted = [];
            for (const num of state.standingsOrder) {
                if (state.drivers[num] && !seen.has(num)) {
                    seen.add(num);
                    sorted.push({ num, teamOrder: teamOrderMap[num] || 0 });
                }
            }
            for (const entry of sortedDefault) {
                if (!seen.has(entry.num)) { seen.add(entry.num); sorted.push(entry); }
            }
        } else {
            sorted = sortedDefault;
        }
        if (!sorted.length) { el.innerHTML = ''; return; }

        // Find the highest lap across all drivers, using the union of
        // every known source (classification, lap times, telemetry on
        // disk). All driver rows render columns 1..maxLap so lap N is
        // visually aligned across the table. Missing laps render as
        // empty slots.
        let maxLap = 0;
        for (const num of Object.keys(state.drivers)) {
            const srcs = [state.lapCls[num], state.lapTimes[num]];
            for (const src of srcs) {
                if (!src) continue;
                for (const k of Object.keys(src)) {
                    const n = parseInt(k);
                    if (n > maxLap) maxLap = n;
                }
            }
            const teleLaps = state.telemetryLaps[num];
            if (teleLaps) for (const n of teleLaps) if (n > maxLap) maxLap = n;
        }
        if (maxLap === 0) maxLap = 1;

        // Qualifying segment layout: per-segment lap-count maxes + per-
        // driver per-lap local position so each driver's Q1 laps line
        // up under each other (similarly for Q2 and Q3), with a one-
        // column gap between segments.
        const hasSegments = Object.values(state.lapSegments)
            .some(segMap => segMap && Object.values(segMap).some(s => s > 0));
        const segMax = { 1: 0, 2: 0, 3: 0 };
        const localPos = {};  // num -> {absoluteLap -> localPos (1-indexed)}
        if (hasSegments) {
            for (const num of Object.keys(state.drivers)) {
                const segs = state.lapSegments[num] || {};
                // Group absolute laps by segment.
                const buckets = { 1: [], 2: [], 3: [] };
                for (const [absStr, seg] of Object.entries(segs)) {
                    const abs = parseInt(absStr);
                    if (seg in buckets) buckets[seg].push(abs);
                }
                localPos[num] = {};
                for (const seg of [1, 2, 3]) {
                    buckets[seg].sort((a, b) => a - b);
                    buckets[seg].forEach((absLap, idx) => {
                        localPos[num][absLap] = idx + 1;
                    });
                    if (buckets[seg].length > segMax[seg]) segMax[seg] = buckets[seg].length;
                }
            }
        }

        // Grid template width:
        //   Qualifying:  TLA + maxQ1 + gap + maxQ2 + gap + maxQ3
        //   else:        TLA + maxLap
        const totalLapCols = hasSegments
            ? (segMax[1] + segMax[2] + segMax[3] + 2)   // +2 for the two gap columns
            : maxLap;

        const allShown = state.hiddenDrivers.size === 0;
        const allBtn = document.getElementById('telemetryAllDrivers');
        if (allBtn) allBtn.classList.toggle('all-selected', allShown);

        let html = `<div class="telemetry-driver-list" style="--max-lap:${totalLapCols}">`;
        for (const { num, teamOrder } of sorted) {
            const d = state.drivers[num];
            const hidden = state.hiddenDrivers.has(num) ? ' hidden' : '';
            const second = teamOrder === 1 ? ' second' : '';
            html += `<div class="telemetry-driver-entry${hidden}${second}" data-driver="${num}" style="--swatch-color:${d.color}">` +
                    `<span class="telemetry-driver-row" data-action="toggle">` +
                    `<span class="telemetry-driver-swatch"></span>` +
                    `<span class="telemetry-driver-tla">${d.tla}</span>` +
                    `</span>` +
                    renderLapList(num, maxLap, { hasSegments, segMax, localPos: localPos[num] || {} }) +
                    `</div>`;
        }
        html += `</div>`;

        // Preserve horizontal scroll across re-renders so the user
        // doesn't get yanked back to lap 1 every time data updates.
        const prevScroll = el.scrollLeft;
        el.innerHTML = html;
        el.scrollLeft = state.lapListScrollLeft != null
            ? state.lapListScrollLeft
            : prevScroll;
        // Re-bind on every render — innerHTML replaced the children
        // including any prior listener targets. The scroll lives on the
        // parent `#telemetryDriverSelector` so a single horizontal
        // scrollbar drives every row in unison.
        el.onscroll = () => { state.lapListScrollLeft = el.scrollLeft; };

        el.querySelectorAll('.telemetry-driver-row[data-action="toggle"]').forEach(row => {
            row.addEventListener('click', (e) => {
                e.stopPropagation();
                const entry = row.closest('.telemetry-driver-entry');
                const num = entry.dataset.driver;
                const wasHidden = state.hiddenDrivers.has(num);
                if (wasHidden) state.hiddenDrivers.delete(num);
                else state.hiddenDrivers.add(num);
                if (wasHidden && (state.mode === 'last' || state.mode === 'best')) {
                    const cmd = state.mode === 'last' ? 'getLastLapTelemetry' : 'getBestLapTelemetry';
                    (state.mode === 'last' ? state.pendingLast : state.pendingBest).add(num);
                    messageBus.send({ cmd, driver: num });
                }
                renderDriverSelector();
                scheduleRender();
            });
        });

        el.querySelectorAll('.telemetry-lap-pill').forEach(pill => {
            pill.addEventListener('click', (e) => {
                e.stopPropagation();
                const driver = pill.dataset.driver;
                const lap = parseInt(pill.dataset.lap);
                if (driver && !isNaN(lap)) toggleLap(driver, lap);
            });
        });

        // The ALL DRIVERS button lives in the tile header and is wired
        // once in init(); only its `all-selected` class is toggled here.
    }

    // Render the recorded-lap list for one driver as small clickable pills.
    // All laps are shown (debug mode) so OUT/IN/PIT/COOL/ABORT traces can
    // be inspected too. Colour mapping:
    //   PUSH → green
    //   LONG → blue
    //   TIMED but no classification yet → white
    //   COOL / ABORT → grey (dimmed)
    //   OUT / IN / PIT → very dim
    // Parse an F1 lap-time string ('M:SS.mmm' or 'SS.mmm') to milliseconds.
    function lapTimeToMs(s) {
        if (typeof s !== 'string') return null;
        const m = s.match(/^(?:(\d+):)?(\d+)\.(\d+)$/);
        if (!m) return null;
        const mins = m[1] ? parseInt(m[1], 10) : 0;
        return mins * 60000 + parseInt(m[2], 10) * 1000
             + parseInt(m[3].padEnd(3, '0').slice(0, 3), 10);
    }

    function renderLapList(num, maxLap, segCtx) {
        const _sType = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
        const _isRaceLike = (_sType === 'race' || _sType === 'sprint');
        // Race mode: hide all lap pills until lights-out (= the
        // pre-race installation lap is classified OUT/PIT by the
        // processor but is NOT a race lap; rendering a pill for it
        // would let the user click into a partial pre-race trace).
        if (_isRaceLike && !state.raceStarted) {
            return '';
        }
        const times = state.lapTimes[num] || {};
        const cls = state.lapCls[num] || {};
        const segs = state.lapSegments[num] || {};
        const teleLaps = state.telemetryLaps[num];
        const useSegLayout = !!(segCtx && segCtx.hasSegments);
        const segMax = segCtx ? segCtx.segMax : null;
        const localPos = segCtx ? segCtx.localPos : {};
        // Segment-start column offsets (0-indexed from TLA col):
        //   seg 1 → col 2
        //   seg 2 → col 2 + maxQ1 + 1 (gap)
        //   seg 3 → col 2 + maxQ1 + 1 + maxQ2 + 1
        const segStartCol = useSegLayout ? {
            1: 2,
            2: 2 + segMax[1] + 1,
            3: 2 + segMax[1] + 1 + segMax[2] + 1,
        } : null;

        // Union of every known lap from every source — show ALL laps
        // (debug mode) so OUT/IN/PIT/COOL/ABORT traces can be inspected,
        // regardless of whether the lap-time stream or the on-disk
        // telemetry list ever reported it.
        const allLaps = new Set();
        for (const k of Object.keys(times)) { const n = parseInt(k); if (!isNaN(n)) allLaps.add(n); }
        for (const k of Object.keys(cls))   { const n = parseInt(k); if (!isNaN(n)) allLaps.add(n); }
        if (teleLaps) for (const n of teleLaps) allLaps.add(n);

        // Per-segment best PUSH and LONG (the segment grouping is
        // implicit in qualifying — pills are placed by absolute lap
        // number so the column N is the same across all drivers).
        const hasSegments = Object.values(segs).some(s => s > 0);
        const segOfLap = (lap) => (hasSegments ? (segs[lap] || 0) : 0);
        const bestPushBySeg = {};
        const bestLongBySeg = {};
        for (const lap of allLaps) {
            const ms = lapTimeToMs(times[lap]);
            if (ms === null) continue;
            const seg = segOfLap(lap);
            const status = cls[lap] || '';
            if (status === 'PUSH' && (!bestPushBySeg[seg] || ms < bestPushBySeg[seg].ms)) bestPushBySeg[seg] = { lap, ms };
            if (status === 'LONG' && (!bestLongBySeg[seg] || ms < bestLongBySeg[seg].ms)) bestLongBySeg[seg] = { lap, ms };
        }

        // SC/VSC + no-data lookups for color precedence.
        const noData = state.lapNoData[num] || new Set();
        const sessionType = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
        const isRace = (sessionType === 'race');

        // In-progress lap = the highest lap with a classification but
        // no lap_time. In P/Q, don't render a pill — can't be selected
        // (lap not complete). In RACE, keep the pill — every lap matters
        // for race-engineer view, and the partial trace is still useful.
        let inProgressLap = null;
        if (!_isRaceLike) {
            for (const lap of allLaps) {
                if (lap in times) continue;
                if (inProgressLap === null || lap > inProgressLap) {
                    inProgressLap = lap;
                }
            }
        }

        // Render every known lap (with allLaps as the set). Layout:
        //   • Practice / Race: column = absoluteLap + 1.
        //   • Qualifying: column = segStartCol[seg] + localPos[lap] - 1
        //     (Q1, gap, Q2, gap, Q3); pill label is the local position.
        let html = '';
        const lapsToRender = useSegLayout ? Array.from(allLaps).sort((a, b) => a - b)
                                          : Array.from({ length: maxLap }, (_, i) => i + 1);
        for (const lap of lapsToRender) {
            let col, pillLabel;
            if (useSegLayout) {
                const seg = segs[lap] || 0;
                const lp = localPos[lap];
                if (!seg || !lp) continue;  // segment unknown — skip
                col = segStartCol[seg] + (lp - 1);
                pillLabel = lp;
            } else {
                col = lap + 1;
                pillLabel = lap;
            }
            if (!allLaps.has(lap) || lap === inProgressLap) {
                html += `<span class="telemetry-lap-empty" style="grid-column:${col}"></span>`;
                continue;
            }
            const status = cls[lap] || '';
            const seg = segOfLap(lap);
            // driverLapClassification type ∈ PUSH / SLOW / OUT / PIT / STOP / "".
            const isIn = (status === 'PIT' || status === 'STOP');
            const isOut = (status === 'OUT');
            const isInOrOut = isIn || isOut;
            // Color precedence (per SME 2026-06-01):
            //   1) IN-pit/STOP/OUT → grey
            //   2) No-data lap → distinct dashed style
            //   3) PUSH (P/Q) or green-flag TIMED (Race) → green
            //   4) SLOW (cool-down) → white
            //   5) anything else → unknown (white)
            // (SC/VSC colouring removed — there was never a source topic.)
            let colorCls;
            if (isInOrOut) colorCls = 'lap-skip';
            else if (noData.has(lap)) colorCls = 'lap-no-data';
            else if (status === 'PUSH') colorCls = 'lap-push';
            else if (isRace && status) colorCls = 'lap-push';   // green-flag race lap
            else if (status === 'SLOW') colorCls = 'lap-cool';
            else colorCls = 'lap-unknown';
            let bestTypeCls = '';
            if (bestPushBySeg[seg] && lap === bestPushBySeg[seg].lap) bestTypeCls = ' lap-best-push';
            else if (bestLongBySeg[seg] && lap === bestLongBySeg[seg].lap) bestTypeCls = ' lap-best-long';
            // Driver's own fastest lap → purple (recomputed each render from
            // state.bestLapNum, so it moves off the old lap automatically).
            if (state.bestLapNum[num] === lap) bestTypeCls += ' lap-purple';
            html += `<span class="telemetry-lap-pill ${colorCls}${bestTypeCls}"`
                  + ` data-driver="${num}" data-lap="${lap}" title="L${lap} ${times[lap] || ''}"`
                  + ` style="grid-column:${col}">${pillLabel}</span>`;
        }
        return html;
    }

    // =========================================================================
    // Legend (right-side overlay showing selected laps)
    // =========================================================================

    function updateDriverBar() {
        const legend = document.getElementById('telemetryLegend');
        if (!legend) return;

        const entries = Object.entries(currentLaps());
        // Selection-only legend: shows the laps currently overlaid on
        // the chart. Hidden whenever the selection is empty OR the
        // chart isn't in selection-rendering mode (last/best/selection).
        if (entries.length === 0 || state.mode === 'live') {
            legend.innerHTML = '';
            legend.classList.remove('has-entries');
            return;
        }

        // Team-order map: second car of each team gets the .second
        // checkered swatch via CSS.
        const teamOrder = {};
        for (const { num, teamOrder: to } of getSortedDrivers()) teamOrder[num] = to;

        legend.classList.add('has-entries');
        let html = '';
        for (const [key, lap] of entries) {
            const isSecond = teamOrder[lap.driver] === 1;
            const secondCls = isSecond ? ' second' : '';
            const status = (state.lapCls[lap.driver] || {})[lap.lap] || '';
            const noData = (state.lapNoData[lap.driver] || new Set()).has(lap.lap);
            const f1Time = (state.lapTimes[lap.driver] || {})[lap.lap] || '';
            // Legend label per SME 2026-06-01:
            //   IN/PIT lap → "IN LAP"
            //   OUT lap   → "OUT LAP"
            //   No data   → "NO DATA " + F1 lap time (if any)
            //   else      → F1 lap time
            let timeLabel;
            if (status === 'IN' || status === 'PIT') timeLabel = 'IN LAP';
            else if (status === 'OUT') timeLabel = 'OUT LAP';
            else if (noData) timeLabel = f1Time ? `NO DATA ${f1Time}` : 'NO DATA';
            else timeLabel = f1Time;
            html += `<div class="telemetry-legend-entry${secondCls}" data-key="${key}" style="--swatch-color:${lap.color}">`
                  + `<span class="telemetry-driver-swatch"></span>`
                  + `<span class="telemetry-legend-tla" style="color:${lap.color}">${lap.tla}</span>`
                  + `<span class="telemetry-legend-lap">L${lap.lap}</span>`
                  + `<span class="telemetry-legend-time">${timeLabel}</span>`
                  + `<button class="telemetry-legend-remove" data-key="${key}" title="Remove">&times;</button>`
                  + `</div>`;
        }
        legend.innerHTML = html;

        legend.querySelectorAll('.telemetry-legend-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const key = btn.dataset.key;
                if (!key) return;
                delete currentLaps()[key];
                renderChart();
                updateDriverBar();
            });
        });
    }

    // =========================================================================
    // Subscribe
    // =========================================================================

    messageBus.on('sessionInfo', (data) => {
        if (data && data.sessionStatus === 'Started') {
            state.raceStarted = true;
        }
    });

    messageBus.on('driverList', (data) => {
        if (!data || typeof data !== 'object') return;
        for (const [num, info] of Object.entries(data)) {
            state.drivers[num] = {
                tla: info.tla || num,
                color: info.teamColour ? `#${info.teamColour}` : (TEAM_COLORS[num] || DEFAULT_CAR_COLOR),
                teamName: info.teamName || '',
            };
        }
        renderDriverSelector();
    });

    // Standings ordering — pre-sorted [{num, position}]. Drives the Last/Best
    // legend order (card 72); other views keep the team/car-number order.
    messageBus.on('standings', (data) => {
        if (!data || !Array.isArray(data.drivers)) return;
        state.standingsOrder = data.drivers.map(e => String(e.num));
        if (state.mode === 'last' || state.mode === 'best') renderDriverSelector();
    });

    // Live trace: the server now emits a fully-decoded per-driver sample
    // (dp = track %, channels, lap) — no more client-side CarData.z decode
    // or position-derived distance. One sample per emit.
    messageBus.on('liveTelemetry:', (topic, data) => {
        const num = topic.split(':')[1];
        if (num) handleLiveTelemetry(num, data);
    });

    messageBus.on('driverStatus:', (topic, data) => {
        const num = topic.split(':')[1];
        if (num) handleDriverStatus(num, data);
    });

    // driverLaps is thin — accumulate the pill-list lap-time map from lastLap
    // as laps arrive. A backward seek wipes state.lapTimes on state:reset and
    // replays the full driverLaps history up to the offset, so the map is
    // rebuilt to exactly the laps that exist at the seek instant.
    messageBus.on('driverLaps:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        if (data.lastLap && data.lastLap.lap != null && data.lastLap.time) {
            (state.lapTimes[num] || (state.lapTimes[num] = {}))[data.lastLap.lap] =
                data.lastLap.time;
        }
        // Per-driver fastest lap → purple pill (server flags bestLap from
        // PersonalFastest/OverallFastest, so it excludes out/in/cool laps).
        const prevBest = state.bestLapNum[num];
        if (data.bestLap && data.bestLap.lap != null) {
            state.bestLapNum[num] = data.bestLap.lap;
        }
        // Last view: refresh this driver's lap when it completes a newer one.
        if (state.mode === 'last' && data.lastLap && data.lastLap.lap != null
                && !state.hiddenDrivers.has(num)
                && state.lastSeenLastLap[num] !== data.lastLap.lap) {
            state.lastSeenLastLap[num] = data.lastLap.lap;
            state.pendingLast.add(num);
            messageBus.send({ cmd: 'getLastLapTelemetry', driver: num });
        }
        // Best view: refresh this driver's lap when its best improves.
        if (state.mode === 'best' && data.bestLap && data.bestLap.lap != null
                && !state.hiddenDrivers.has(num) && data.bestLap.lap !== prevBest) {
            state.pendingBest.add(num);
            messageBus.send({ cmd: 'getBestLapTelemetry', driver: num });
        }
        renderDriverSelector();
    });

    // driverLapClassification:{num} {lap, trackPct, type}. Builds the
    // per-lap class map incrementally (no .laps snapshot in the new topic,
    // and no per-lap Q-segment — pill grouping by Q-segment is dropped).
    messageBus.on('driverLapClassification:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data || data.lap == null) return;
        const map = state.lapCls[num] || {};
        map[data.lap] = data.type;
        state.lapCls[num] = map;
        renderDriverSelector();
    });

    messageBus.on('telemetryLap:', (topic, data, offset_ms) => {
        handleLapTelemetry(topic, data);
    });

    // Server tells us which laps actually have a telemetry row on disk
    // so the lap-pill list only shows clickable pills for laps that can
    // actually render a trace. Payload: {driverNum: [lap, lap, ...]}.
    messageBus.on('telemetryAvailable', (data) => {
        if (!data || typeof data !== 'object') return;
        for (const [num, laps] of Object.entries(data)) {
            if (Array.isArray(laps)) state.telemetryLaps[num] = new Set(laps);
        }
        renderDriverSelector();
    });

    messageBus.on('trackGeometry', (data) => {
        if (data && Array.isArray(data.corners)) {
            state.corners = data.corners;
            renderCornerLabels();
            scheduleRender();
        }
        if (data && Array.isArray(data.sectors)) {
            state.sectorRanges = data.sectors;     // [{sector, startPct, endPct}, …]
            scheduleRender();
        }
    });
    // Track yellow / double-yellow marshal sectors on the x-axis as a
    // vertical band. Cleared per SME by 'track clear in sector X' RCMs;
    // the yellowFlag topic carries the cumulative current set so we just
    // mirror it.
    messageBus.on('yellowFlag', (data) => {
        state.yellowSectors = Array.isArray(data) ? data.slice() : [];
        scheduleRender();
    });

    // Draw yellow vertical bands for sectors currently under yellow.
    // Per SME 2026-06-06: only sector-yellow flags translate to a
    // telemetry highlight (= no global red / green / SC / VSC band).
    function drawYellowSectors(ctx, margin, plotW, plotH) {
        if (!state.yellowSectors || !state.yellowSectors.length) return;
        if (!state.sectorRanges || !state.sectorRanges.length) return;
        const byNum = new Map(state.sectorRanges.map(s => [s.sector, s]));
        ctx.save();
        ctx.fillStyle = 'rgba(255, 215, 0, 0.18)';
        for (const num of state.yellowSectors) {
            const range = byNum.get(num);
            if (!range) continue;
            const x1 = pctToX(range.startPct, margin.left, plotW);
            const x2 = pctToX(range.endPct, margin.left, plotW);
            if (x2 > x1) ctx.fillRect(x1, margin.top, x2 - x1, plotH);
        }
        ctx.restore();
    }

    // Populate the .telemetry-x-axis strip with corner labels positioned
    // in pixel space so they line up with the chart's internal plot
    // region (which has a 45px left margin and 10px right margin).
    function renderCornerLabels() {
        const el = document.getElementById('telemetryXAxis');
        if (!el || !state.canvas) return;
        if (!state.corners || state.corners.length === 0) {
            el.innerHTML = '';
            return;
        }
        const cssW = state.canvas.clientWidth;
        const leftMargin = 45, rightMargin = 10;
        const plotW = Math.max(0, cssW - leftMargin - rightMargin);

        // Compute px for each corner first, then sort by px so we can
        // detect overlaps in display order (corners aren't always
        // numbered in track order, but their pct values are).
        // Project each corner using the same zoom window the canvas uses
        // so corner labels stay aligned with the dashed corner markers.
        const items = [];
        for (const c of state.corners) {
            const pct = Number(c.pct);
            if (!isFinite(pct) || pct < state.xMin || pct > state.xMax) continue;
            items.push({ px: pctToX(pct, leftMargin, plotW), number: c.number ?? '' });
        }
        items.sort((a, b) => a.px - b.px);

        // Overlap rule: keep ALL labels on a single row. When a label
        // would land within MIN_GAP_PX of the previous one, nudge it to
        // the right so they sit side-by-side at exactly MIN_GAP_PX.
        // The dashed marker line stays at the true distPct; only the
        // label text shifts so dense corner sequences read cleanly.
        const MIN_GAP_PX = 22;
        let lastPx = -Infinity;
        let html = '';
        for (const it of items) {
            let labelPx = it.px;
            if (labelPx - lastPx < MIN_GAP_PX) {
                labelPx = lastPx + MIN_GAP_PX;
            }
            lastPx = labelPx;
            html += `<span class="corner-label" style="left: ${labelPx}px">${it.number}</span>`;
        }
        el.innerHTML = html;
    }

    messageBus.on('state:reset', () => {
        state.liveSamples = {};
        state.liveLap = {};
        state.driverStatus = {};
        state.lapTimes = {};
        state.lapCls = {};
        state.lapSegments = {};
        state.lapNoData = {};
        state.telemetryLaps = {};
        // Last/Best are view snapshots — clear on reset. Selection is the
        // user's set and SURVIVES seek/restore; "future" laps are pruned on
        // state:seek-complete below (backward seek only).
        state.lastLaps = {};
        state.bestLaps = {};
        state.pendingSelection.clear();
        state.pendingLast.clear();
        state.pendingBest.clear();
        state.lastSeenLastLap = {};
        renderDriverSelector();
    });

    // After a seek, drop Selection laps that no longer exist at the new clock
    // (backward seek → "future" laps). lapTimes/telemetryLaps have been rebuilt
    // by the restore replay by the time this fires; a forward seek keeps all.
    messageBus.on('state:seek-complete', () => {
        for (const k of Object.keys(state.selectionLaps)) {
            const { driver, lap } = state.selectionLaps[k];
            const known = (state.lapTimes[driver] && lap in state.lapTimes[driver])
                || (state.telemetryLaps[driver] && state.telemetryLaps[driver].has(lap));
            if (!known) delete state.selectionLaps[k];
        }
        renderChart();
        updateDriverBar();
    });

    document.addEventListener('DOMContentLoaded', init);

})();
