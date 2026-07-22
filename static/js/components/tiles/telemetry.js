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
    const _ST = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
    const IS_RACE = _ST === 'race' || _ST === 'sprint';   // race dashboard mode (card J3V1CFdS)
    const CHANNELS = {
        speed:         { idx: 1, label: 'Speed (km/h)', min: 0, max: 360 },
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
        lapStartMs: {},       // num -> playback offset (ms) at the current lap's S/F crossing (stopwatch)
        dashInfo: {},         // num -> dashInfo {blank, running, stopwatchMs, delta, position, flag} (P/Q, server)
        driverStatus: {},     // num -> "PIT"|"OUT"|"TRACK"|...
        lapTimes: {},         // num -> {lapNum -> "1:23.456"}
        lapCls: {},           // num -> {lapNum -> type}
        bestLapNum: {},       // num -> driver's fastest lap number (driverLaps.bestLap) → purple pill
        lapSegments: {},      // num -> {lapNum -> qualPart 1/2/3} (from driverLaps.lastLap.part) — card 66
        eliminated: new Set(),// car nums knocked out (from qualifyingSegment) — card 71
        qualifyingPart: 0,    // current quali part 1/2/3 (0 = not quali / unknown)
        activePart: 0,        // pill-tab selected part (defaults to current part) — card 66
        lapNoData: {},        // num -> Set(lapNum) — laps that came in empty
        telemetryLaps: {},    // num -> Set(laps that have telemetry on disk)
        hiddenDrivers: new Set(),
        view: 'dashboard',    // default view (card 280); 'telemetry' (trace chart) | 'dashboard' (live gauges)
        dashSlots: [null, null],  // driver nums in the 2 dashboard gauges (pos1 left, pos2 right)
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
        if (_telPlaying && (state.view === 'dashboard' || state.mode === 'live')) return;   // animateLive covers it
        if (state.renderPending) return;
        state.renderPending = true;
        requestAnimationFrame(() => {
            state.renderPending = false;
            if (state.view === 'dashboard') renderDashboard(); else renderChart();
        });
    }

    // --- smooth live marker/trace (interpolation, mirrors track_map) ---
    // Live samples arrive at ~3.7Hz; render the marker and the trace's leading edge each
    // frame at (playback clock − LAG) interpolated between bracketing samples so they
    // glide instead of snapping. LAG guarantees the "next" sample is already buffered.
    // Only runs in live-follow mode while playing.
    const TEL_LAG_MS = 500;
    let _telPlaying = false;

    function telRenderNowMs() {
        const ct = messageBus.clockTime, st = messageBus.startTime;
        if (!ct || !st) return null;
        return (ct.getTime() - st.getTime()) - TEL_LAG_MS;
    }

    // Live samples ([dp,speed,rpm,gear,throttle,brake,offsetMs] ascending by offsetMs)
    // truncated + interpolated to render time t (ms from start). Returns {trace, marker}.
    function liveViewAt(samples, t) {
        const last = samples[samples.length - 1];
        if (t == null || last[6] == null || t >= last[6]) return { trace: samples, marker: last };
        if (t <= samples[0][6]) return { trace: [samples[0]], marker: samples[0] };
        let i = samples.length - 1;
        while (i > 0 && samples[i][6] > t) i--;
        const a = samples[i], b = samples[i + 1];
        const f = (t - a[6]) / (b[6] - a[6]);
        const m = a.slice();
        for (let k = 0; k < 6; k++) if (k !== 3) m[k] = a[k] + (b[k] - a[k]) * f;   // gear (idx 3) discrete
        m[6] = t;
        return { trace: samples.slice(0, i + 1).concat([m]), marker: m };
    }

    function animateLive() {
        if (_telPlaying) {
            if (state.view === 'dashboard') renderDashboard();
            else if (state.mode === 'live') renderChart();
        }
        requestAnimationFrame(animateLive);
    }
    requestAnimationFrame(animateLive);

    messageBus.on('playback:status', (data) => {
        if (data) { _telPlaying = !!data.isPlaying; scheduleRender(); }
    });

    // ═══════════════════════ Live dashboard view ═══════════════════════
    // 2–3 driver gauges (speed dial 0–360, throttle/brake arc, gear tape) that replace
    // the trace chart on demand. Fed by the same interpolated liveSamples, so the speed
    // sweeps smoothly between the ~3.7Hz samples (renderDashboard runs every rAF frame).
    const D_A0 = 215, D_A1 = 360, D_B0 = 0, D_B1 = 145;   // throttle 215→360, brake 0→145
    // D_CELL = gear-cell stride (% of the tape row). Narrow enough that the prev/next gears show
    // faded at the window edges (glass effect) while the current gear sits centred. (card 277)
    const D_SPAN = (D_A1 - D_A0) + (D_B1 - D_B0), D_VMAX = 360, D_CELL = 34;
    let _dashPanels = [], _dashBuilt = false, _dashSelBar = null, _dashMidInfo = [], _dashPushEls = {}, _dashMapCv = null, _dashRace = null;
    // Auto-select (card wfMzaSwh): server recommends the pair (already debounced ~3 s server-side);
    // apply it as it arrives while the toggle is ON. Not persisted.
    let _autoOn = true, _autoPair = [], _autoBtn = null;
    const autoNorm = p => { const s = (p || []).filter(Boolean).map(String).slice(0, 2); return [s[0] || null, s[1] || null]; };

    const dPt = (cx, cy, r, d) => { const t = d * Math.PI / 180; return [cx + r * Math.sin(t), cy - r * Math.cos(t)]; };
    function dArc(cx, cy, r, d0, d1) {
        const [x0, y0] = dPt(cx, cy, r, d0), [x1, y1] = dPt(cx, cy, r, d1);
        const large = Math.abs(d1 - d0) > 180 ? 1 : 0, sweep = d1 > d0 ? 1 : 0;
        return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${large} ${sweep} ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    }
    function dashInk(hex) {
        const c = (hex || '').replace('#', ''); if (c.length < 6) return '#fff';
        const l = parseInt(c.slice(0, 2), 16) * .299 + parseInt(c.slice(2, 4), 16) * .587 + parseInt(c.slice(4, 6), 16) * .114;
        // 128 midpoint (was 150) — bright team colours like Mercedes green (#00D7B6, ~147) take dark
        // text; matches getContrastColor used elsewhere.
        return l > 128 ? '#0b0d10' : '#fff';
    }
    // Manual driver selection (card 277): two gauge slots, pos1 (left) + pos2 (right). Selecting a
    // NEW driver puts them in pos2 and promotes the current pos2 → pos1 (oldest drops off); clicking
    // an already-selected driver unselects it. Same for every session (P/Q + race). Driven by the
    // top selector, and by standings / track-map clicks via F1Dashboard.focus. (user)
    function dashToggle(num) {
        if (_autoOn) { _autoOn = false; syncAutoBtn(); }   // a manual pick turns auto-select off (card)
        num = String(num);
        if (!state.drivers[num]) return;
        let sel = state.dashSlots.filter(Boolean);
        const at = sel.indexOf(num);
        if (at >= 0) sel.splice(at, 1);          // already selected → unselect
        else sel.push(num);                       // new → append (pos2); left-pack keeps 2 max
        sel = sel.slice(-2);
        state.dashSlots = [sel[0] || null, sel[1] || null];
        if (_dashBuilt) { paintDashSlot(0); paintDashSlot(1); paintDashSelector(); }
    }

    // ── auto-select apply (card wfMzaSwh) ──
    function applyAuto(pair) {
        const s = autoNorm(pair);
        state.dashSlots = s;
        if (_dashBuilt) { paintDashSlot(0); paintDashSlot(1); paintDashSelector(); }
    }
    // Called each dashboard frame: switch to the latest recommendation, but only once ≥3 s of
    // playback have passed since the previous switch (anti-flicker). Runs via a code path separate
    // from dashToggle so it never flips the toggle off.
    function autoTick() {
        if (!_autoOn) return;
        const want = autoNorm(_autoPair);
        if (want[0] === state.dashSlots[0] && want[1] === state.dashSlots[1]) return;
        applyAuto(want);   // debounce lives server-side; apply the recommendation as it arrives
    }
    function syncAutoBtn() { if (_autoBtn) _autoBtn.classList.toggle('on', _autoOn); }
    function toggleAuto() {
        _autoOn = !_autoOn; syncAutoBtn();
        if (_autoOn) autoTick();   // enabling → apply the current recommendation
    }

    function buildDashboard() {
        const cont = document.getElementById('dashboardContainer');
        if (!cont) return;
        cont.innerHTML = ''; _dashPanels = [];
        // Top driver selector (card 277) — click a driver to select/unselect it.
        _dashSelBar = document.createElement('div'); _dashSelBar.className = 'dash-selbar';
        cont.appendChild(_dashSelBar);
        // Two gauge panels (pos1 left, pos2 right) with a reserved middle for the zoomed
        // track map (added later, card 277).
        const panels = document.createElement('div'); panels.className = 'dash-panels';
        cont.appendChild(panels);
        let ticks = '';
        for (const s of [60, 120, 180, 240, 300, 360]) {
            let d = D_A0 + D_SPAN * (s / D_VMAX); if (d > 360) d -= 360;
            const [tx, ty] = dPt(120, 120, 118, d);
            ticks += `<text class="dash-tick" x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="middle" dominant-baseline="middle">${s}</text>`;
        }
        const mkPanel = (idx) => {
            const el = document.createElement('div'); el.className = 'dash-panel';
            el.innerHTML = `
                <div class="dash-gauge">
                    <svg viewBox="0 0 240 240" aria-hidden="true">
                        <path class="dash-track" stroke-width="12" d="${dArc(120,120,105,D_A0,D_A1)} ${dArc(120,120,105,D_B0,D_B1)}"/>
                        <path class="dash-track" stroke-width="16" d="${dArc(120,120,84,D_A0,D_A1)}"/>
                        <path class="dash-track" stroke-width="16" d="${dArc(120,120,84,D_B0,D_B1)}"/>
                        <path class="dash-fill d-spd" stroke="var(--c-blue)"  stroke-width="12" d=""/>
                        <path class="dash-fill d-thr" stroke="var(--c-green)" stroke-width="16" d=""/>
                        <path class="dash-fill d-brk" stroke="var(--c-red)"   stroke-width="16" d=""/>
                        ${ticks}
                    </svg>
                    <div class="dash-readout">
                        <div class="dash-speed d-v">0</div>
                        <div class="dash-kmh">KM/H</div>
                        <div class="dash-tape"><div class="dash-tape-mark"></div><div class="dash-tape-row d-tape"></div></div>
                    </div>
                </div>
                <div class="dash-tla d-tla"></div>`;
            const row = el.querySelector('.d-tape');
            // Order R N 1 2 … 8. gear=0 → N; R (=-1) isn't in the feed but is shown for completeness.
            for (const gv of [-1, 0, 1, 2, 3, 4, 5, 6, 7, 8]) {
                const c = document.createElement('div'); c.className = 'dash-tape-cell';
                c.textContent = gv === -1 ? 'R' : gv === 0 ? 'N' : gv; c.dataset.g = gv; row.appendChild(c);
            }
            const p = {
                el, spd: el.querySelector('.d-spd'), thr: el.querySelector('.d-thr'), brk: el.querySelector('.d-brk'),
                v: el.querySelector('.d-v'), tapeRow: row, cells: row.querySelectorAll('.dash-tape-cell'),
                tla: el.querySelector('.d-tla'),
            };
            return p;
        };
        const p0 = mkPanel(0); panels.appendChild(p0.el); _dashPanels.push(p0);
        // Middle: 2×2 grid — top row reserved for the zoomed track maps (later), bottom row is the
        // per-driver info tile (stopwatch + delta/classification), left = pos1, right = pos2. (card 277)
        const mid = document.createElement('div'); mid.className = 'dash-mid';
        if (IS_RACE) {
            // Race: top = zoomed track mini-map; bottom = combined Int/position panel. (card J3V1CFdS)
            // Row 1: P{n} | Int | P{n} (positions + gap aligned horizontally); row 2: statuses.
            // Row 1: TLA | (empty) | TLA; row 2: P{n} | Int | P{n}; row 3: statuses.
            mid.innerHTML = `
                <div class="dm-map dm-map-race"></div>
                <div class="dm-race">
                    <div class="dr-tla" data-side="0"></div>
                    <div></div>
                    <div class="dr-tla" data-side="1"></div>
                    <div class="dr-pos" data-side="0"></div>
                    <div class="dr-gap"></div>
                    <div class="dr-pos" data-side="1"></div>
                    <div class="dr-status" data-side="0"></div>
                    <div></div>
                    <div class="dr-status" data-side="1"></div>
                </div>`;
            _dashRace = {
                map: mid.querySelector('.dm-map'),
                gap: mid.querySelector('.dr-gap'),
                sides: [0, 1].map(i => ({
                    tla: mid.querySelector(`.dr-tla[data-side="${i}"]`),
                    pos: mid.querySelector(`.dr-pos[data-side="${i}"]`),
                    status: mid.querySelector(`.dr-status[data-side="${i}"]`),
                })),
            };
            _dashMapCv = null; _dashMidInfo = [];
            if (window.F1TrackMap && window.F1TrackMap.mountMini) window.F1TrackMap.mountMini(_dashRace.map);
        } else {
            const infoHtml = (slot) => `<div class="dm-info" data-slot="${slot}">` +
                `<div class="dm-flag"></div>` +
                `<div class="dm-topline"><span class="dm-tla"></span><span class="dm-watch">-:--.-</span></div>` +
                `<div class="dm-label"></div>` +
                `<div class="dm-delta"></div>` +
                `<div class="dm-pos"></div></div>`;
            mid.innerHTML = `
                <div class="dm-map"><canvas class="dm-map-cv"></canvas></div>
                ${infoHtml(0)}${infoHtml(1)}`;
            _dashMapCv = mid.querySelector('.dm-map-cv');
            _dashMidInfo = [...mid.querySelectorAll('.dm-info')].map(el => ({
                el, flag: el.querySelector('.dm-flag'), tla: el.querySelector('.dm-tla'), watch: el.querySelector('.dm-watch'),
                label: el.querySelector('.dm-label'), delta: el.querySelector('.dm-delta'), pos: el.querySelector('.dm-pos'),
            }));
        }
        panels.appendChild(mid);
        const p1 = mkPanel(1); panels.appendChild(p1.el); _dashPanels.push(p1);
        _dashBuilt = true;
        refreshDashDrivers();
    }

    // Rebuild the top selector (current running order, else team order) + repaint both slots.
    function paintDashSelector() {
        if (!_dashSelBar) return;
        const order = _dashOrder.length ? _dashOrder : getSortedDrivers().map(d => d.num);
        _dashSelBar.innerHTML = ''; _dashPushEls = {};
        order.forEach(num => {
            const d = state.drivers[num]; if (!d) return;
            const cell = document.createElement('div'); cell.className = 'dash-selcell';
            const b = document.createElement('button'); b.className = 'dash-selbtn'; b.dataset.num = num;
            b.textContent = d.tla || num;
            // CSS drives fill/border/text per state from these; ink = contrast for the selected fill.
            b.style.setProperty('--team', d.color || '#888');
            b.style.setProperty('--ink', dashInk(d.color));
            if (state.dashSlots.includes(num)) b.classList.add('sel');
            b.addEventListener('click', () => dashToggle(num));
            cell.appendChild(b);
            // PUSH marker under the button (not clickable) — green while this driver's current lap is PUSH.
            const push = document.createElement('div'); push.className = 'dash-selpush';
            cell.appendChild(push); _dashPushEls[num] = push;
            _dashSelBar.appendChild(cell);
        });
        paintDashPush();
    }

    // Colour the marker under each selector button. P/Q: green while the driver's current lap is
    // PUSH. Race: server indicator (green = Int < 1 s / orange = PIT-OUT). (card J3V1CFdS)
    function paintDashPush() {
        for (const num in _dashPushEls) {
            let colour = null;
            if (IS_RACE) {
                colour = (state.dashInfo[num] || {}).indicator || null;
            } else if ((state.lapCls[num] || {})[state.liveLap[num]] === 'PUSH') {
                colour = 'green';
            }
            const el = _dashPushEls[num];
            el.classList.toggle('push', colour === 'green');
            el.classList.toggle('orange', colour === 'orange');
        }
    }

    function refreshDashDrivers() {
        if (!_dashBuilt) return;
        paintDashSelector();
        paintDashSlot(0); paintDashSlot(1);
    }

    function paintDashSlot(i) {
        const p = _dashPanels[i]; if (!p) return;
        const num = state.dashSlots[i], d = num && state.drivers[num];
        p.el.classList.toggle('empty', !d);
        p.tla.style.background = d ? d.color : 'transparent';
        p.tla.style.color = d ? dashInk(d.color) : '';
        p.tla.textContent = d ? d.tla : '---';
    }

    function renderDashboard() {
        if (!_dashBuilt) return;
        autoTick();   // apply the (debounced) auto-select recommendation
        const t = telRenderNowMs();
        _dashPanels.forEach((p, i) => {
            const num = state.dashSlots[i];
            const samples = num && state.liveSamples[num];
            let spd = 0, thr = 0, brk = 0, gear = 1;
            if (samples && samples.length && !liveTraceSuppressed(num)) {
                const m = liveViewAt(samples, t).marker;
                spd = m[1] || 0; gear = m[3] == null ? 1 : m[3];   // 0 = Neutral (don't coerce to 1)
                thr = Math.max(0, Math.min(1, (m[4] || 0) / 100));
                brk = Math.max(0, Math.min(1, (m[5] || 0) / 100));
            }
            p.thr.setAttribute('d', dArc(120, 120, 84, D_A0, D_A0 + (D_A1 - D_A0) * thr));
            p.brk.setAttribute('d', dArc(120, 120, 84, D_B1 - (D_B1 - D_B0) * brk, D_B1));
            p.spd.setAttribute('d', dArc(120, 120, 105, D_A0, D_A0 + D_SPAN * (Math.min(spd, D_VMAX) / D_VMAX)));
            p.v.textContent = Math.round(spd);
            const g = Math.max(-1, Math.min(8, Math.round(gear)));   // -1 = R, 0 = N
            // cells are [R,N,1..8]; the current gear g sits at index g+1 (N=idx1, gear1=idx2, …).
            p.tapeRow.style.transform = `translateX(${50 - (g + 1.5) * D_CELL}%)`;
            p.cells.forEach(c => c.classList.toggle('cur', +c.dataset.g === g));
        });
        if (IS_RACE) {
            renderRaceInfo();
        } else {
            renderDashInfo(0); renderDashInfo(1);
            renderDashMap();
        }
        paintDashPush();
    }

    // Race center-bottom panel: each selected driver's P{n} (+ PIT/OUT status), and the gap between
    // the two (Int, or Int-chain when non-adjacent) coloured by the chaser's Int band. Mini-map
    // follows the chaser (worse race position). (card J3V1CFdS)
    function renderRaceInfo() {
        const R = _dashRace; if (!R) return;
        if (R.map) R.map.style.display = state.dashSlots.some(Boolean) ? '' : 'none';   // no selection → no map
        const infos = state.dashSlots.map(num => (num && state.dashInfo[num]) || null);
        R.sides.forEach((side, i) => {
            const num = state.dashSlots[i], di = infos[i];
            if (!num || !state.drivers[num] || !di) {
                side.tla.textContent = '';
                side.pos.textContent = ''; side.pos.className = 'dr-pos';
                side.status.innerHTML = ''; side.status.className = 'dr-status'; side._key = '';
                return;
            }
            side.tla.textContent = state.drivers[num].tla || num;
            side.pos.textContent = di.position != null ? `P${di.position}` : '';
            side.pos.className = 'dr-pos';
            // status line: PIT replaces the tyre with "PIT"; otherwise (on track OR out-lap) the
            // current tyre + age — the tyre comes back once the driver is OUT. (SME 2026-07-15)
            let key, html, cls;
            if (di.status === 'PIT') {
                key = 'PIT'; html = 'PIT'; cls = 'dr-status st-pit';
            } else if (di.tyreCompound) {
                const comp = String(di.tyreCompound).toLowerCase();
                key = `T${comp}${di.tyreNew ? 1 : 0}${di.tyreAge}`;
                html = `<img class="dr-tyre" src="/static/images/tyres/${comp}-${di.tyreNew ? 'new' : 'used'}.svg" alt="">` +
                       `<span class="dr-tyre-age">${di.tyreAge != null ? di.tyreAge : ''}</span>`;
                cls = 'dr-status st-tyre';
            } else { key = ''; html = ''; cls = 'dr-status'; }
            if (side._key !== key) {   // only touch the DOM when it changes (renders every frame)
                side.status.innerHTML = html; side.status.className = cls; side._key = key;
            }
        });
        // gap between the two + chaser (worse position)
        let gapText = '', gapCls = 'dr-gap', chaser = null;
        if (state.dashSlots.every(Boolean) && infos.every(d => d && d.position != null)) {
            const pL = infos[0].position, pR = infos[1].position;   // left slot / right slot positions
            chaser = pL >= pR ? state.dashSlots[0] : state.dashSlots[1];
            const ms = raceGapMs(Math.max(pL, pR), Math.min(pL, pR));
            if (ms != null) {
                // Unsigned magnitude — the positions on either side show who's ahead. (SME)
                gapText = (ms / 1000).toFixed(3);
                const col = (state.dashInfo[chaser] || {}).intColour;
                if (col) gapCls += ` c-${col}`;
            } else {
                gapText = ((state.dashInfo[chaser] || {}).intText || '—').replace(/^\+/, '');   // lapped / unknown
            }
        }
        R.gap.textContent = gapText; R.gap.className = gapCls;
        const focus = chaser || state.dashSlots.filter(Boolean).slice(-1)[0] || null;
        if (window.F1TrackMap && window.F1TrackMap.setMiniFocus) window.F1TrackMap.setMiniFocus(focus);
    }

    // Int-chain: sum each driver's interval-to-car-ahead over positions (leaderPos, chaserPos].
    // Null if any link is missing/lapped (can't chain).
    function raceGapMs(chaserPos, leaderPos) {
        const byPos = {};
        for (const n in state.dashInfo) {
            const di = state.dashInfo[n];
            if (di && di.position != null) byPos[di.position] = di.intMs;
        }
        let sum = 0;
        for (let p = leaderPos + 1; p <= chaserPos; p++) {
            const iv = byPos[p];
            if (iv == null) return null;
            sum += iv;
        }
        return sum;
    }

    // Mini live speed-trace viewer (card 287): a scaled-down clone of the large telemetry chart —
    // both selected drivers' live SPEED traces over the full lap, 60 km/h grid + y labels, corner
    // markers/numbers, plot bbox. Display-only (no zoom/pan). One wide canvas spanning the mid row.
    function renderDashMap() {
        const cv = _dashMapCv; if (!cv) return;
        const dpr = window.devicePixelRatio || 1;
        const cw = cv.clientWidth, ch = cv.clientHeight;
        if (cw <= 0 || ch <= 0) return;
        if (cv.width !== Math.round(cw * dpr) || cv.height !== Math.round(ch * dpr)) {
            cv.width = Math.round(cw * dpr); cv.height = Math.round(ch * dpr);
        }
        const ctx = cv.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cw, ch);
        const margin = { top: 8, right: 8, bottom: 8, left: 30 };
        const plotW = cw - margin.left - margin.right, plotH = ch - margin.top - margin.bottom;
        if (plotW <= 0 || plotH <= 0) return;
        const yMin = CHANNELS.speed.min, yMax = CHANNELS.speed.max, range = yMax - yMin || 1;

        // Lock the x-window to the full lap so the shared pctToX helpers (drawTrace/drawCornerMarkers)
        // project 0–100% across the mini chart; restore after (no zoom/pan here).
        const sMin = state.xMin, sMax = state.xMax;
        state.xMin = 0; state.xMax = 100;
        try {
            // horizontal speed grid every 60 km/h + y labels
            ctx.strokeStyle = 'rgba(255,255,255,0.08)'; ctx.lineWidth = 0.5;
            ctx.fillStyle = '#666'; ctx.font = '9px Monaco, Consolas, monospace';
            ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
            for (let v = 0; v <= yMax; v += 60) {
                const y = margin.top + (1 - (v - yMin) / range) * plotH;
                ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(margin.left + plotW, y); ctx.stroke();
                ctx.fillText(v, margin.left - 3, y);
            }
            ctx.textBaseline = 'alphabetic';
            // plot bbox
            ctx.strokeStyle = 'rgba(255,255,255,0.18)'; ctx.lineWidth = 1;
            ctx.strokeRect(margin.left, margin.top, plotW, plotH);
            // corner markers (dashed verticals) + numbers
            drawCornerMarkers(ctx, margin.left, margin.top, plotW, plotH);
            drawMiniCornerNumbers(ctx, margin.left, margin.top, plotW, plotH);
            // both selected drivers' live speed traces (2nd car of a team → dashed)
            const teamOrder = {};
            for (const { num, teamOrder: to } of getSortedDrivers()) teamOrder[num] = to;
            for (const num of state.dashSlots) {
                if (!num) continue;
                const samples = state.liveSamples[num];
                if (!samples || !samples.length || liveTraceSuppressed(num)) continue;
                const d = state.drivers[num], color = d ? d.color : DEFAULT_CAR_COLOR;
                const view = liveViewAt(samples, telRenderNowMs());
                drawTrace(ctx, view.trace, color, 'speed', margin, plotW, plotH, yMin, yMax, teamOrder[num] === 1);
                drawMiniMarker(ctx, view.marker, color, d ? d.tla : num, margin, plotW, plotH, yMin, yMax);
            }
        } finally {
            state.xMin = sMin; state.xMax = sMax;
        }
    }

    // Small leading-edge TLA marker for the mini chart (scaled down from drawMarker).
    function drawMiniMarker(ctx, sample, color, tla, margin, plotW, plotH, yMin, yMax) {
        if (!sample || sample[1] == null || sample[0] < 0 || sample[0] > 100) return;
        const x = pctToX(sample[0], margin.left, plotW);
        const y = margin.top + (1 - (sample[1] - yMin) / (yMax - yMin)) * plotH;
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = '#fff'; ctx.stroke();
        ctx.fillStyle = getContrastColor(color); ctx.font = 'bold 7px Monaco, Consolas, monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(tla, x, y);
        ctx.textBaseline = 'alphabetic';
    }

    // Corner numbers on the mini chart (top of each dashed marker), left-to-right overlap nudge.
    function drawMiniCornerNumbers(ctx, left, top, plotW, plotH) {
        if (!state.corners || state.corners.length === 0) return;
        const items = [];
        for (const c of state.corners) {
            const pct = Number(c.pct);
            if (isFinite(pct)) items.push({ px: pctToX(pct, left, plotW), n: c.number ?? '' });
        }
        items.sort((a, b) => a.px - b.px);
        ctx.fillStyle = 'rgba(255,213,0,0.9)'; ctx.font = '8px Monaco, Consolas, monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        const MIN_GAP = 10; let last = -Infinity;
        for (const it of items) {
            let x = it.px; if (x - last < MIN_GAP) x = last + MIN_GAP; last = x;
            ctx.fillText(it.n, x, top + 1);
        }
        ctx.textBaseline = 'alphabetic';
    }

    // Lap stopwatch M:SS.s — dp decimals (live = tenths, frozen = ms).
    function fmtWatch(ms, dp) {
        dp = dp || 1;
        const t = Math.max(0, ms) / 1000, m = Math.floor(t / 60), s = t - m * 60;
        return `${m}:${s < 10 ? '0' : ''}${s.toFixed(dp)}`;
    }

    // Middle info tile — pure renderer of the server's dashInfo:{num} (P/Q). Stopwatch = the current
    // lap's running time (client ticks its own S/F anchor, resetting each lap); the middle line is the
    // projected lap time (predicting) then the actual (observed); the bottom line is the position.
    // (card Z4PfDRry)
    function renderDashInfo(i) {
        const info = _dashMidInfo[i]; if (!info) return;
        const num = state.dashSlots[i];
        const di = num && state.dashInfo[num];
        if (!num || !state.drivers[num] || !di) {
            info.el.classList.add('empty');
            info.flag.className = 'dm-flag';
            info.tla.textContent = '';
            info._run = null; info._fc = false;
            info.watch.textContent = '-:--.-'; info.watch.className = 'dm-watch';
            info.label.textContent = '';
            info.delta.textContent = ''; info.delta.className = 'dm-delta';
            info.pos.textContent = ''; info.pos.className = 'dm-pos';
            return;
        }
        info.el.classList.remove('empty');
        info.tla.textContent = state.drivers[num].tla || num;
        // stopwatch: ticks during the forecast (predicting); on lap end (server → observed, label
        // "LAP TIME") it FREEZES at the lap's final value rather than resetting to 0. (SME 2026-07-15)
        const forecasting = di.lapTimeLabel === 'FORECAST';
        // telRenderNowMs() is null before the clock is set; guard it so the
        // stopwatch shows its placeholder, not 0:00.0 from null-coerced-to-0
        // arithmetic. (NHB0WQA6)
        const nowMs = telRenderNowMs();
        const eNow = (di.running && state.lapStartMs[num] != null && nowMs != null)
            ? (nowMs - state.lapStartMs[num]) : null;
        if (forecasting && eNow != null) {
            if (!info._fc) info._run = null;   // new forecast session → start fresh
            info._fc = true;
            // monotonic within a session: ignore a >1 s drop (lap-anchor reset before the lap-end),
            // so the frozen value is the completed lap's time, not ~0.
            info._run = (info._run != null && eNow < info._run - 1000) ? info._run : eNow;
            info.watch.textContent = fmtWatch(info._run, 1);
        } else {
            info._fc = false;
            if (di.lapTimeLabel === 'LAP TIME' && info._run != null) {
                info.watch.textContent = fmtWatch(info._run, 1);   // frozen at lap end
            } else {
                info._run = null;
                info.watch.textContent = '-:--.-';
            }
        }
        info.watch.className = 'dm-watch';
        // section title: LAP TIME FORECAST (forecast) / LAP TIME (observed)
        info.label.textContent = di.lapTimeLabel || '';
        // lap time — projected (predicting, 1dp) or actual (observed, 3dp)
        if (di.lapTime) {
            info.delta.textContent = di.lapTime.ms != null ? fmtWatch(di.lapTime.ms, di.lapTime.dp || 1) : '-:--.-';
            info.delta.className = 'dm-delta' + (di.lapTime.colour ? ` c-${di.lapTime.colour}` : '');
        } else { info.delta.textContent = ''; info.delta.className = 'dm-delta'; }
        // position
        if (di.position) {
            info.pos.textContent = di.position.text;
            info.pos.className = 'dm-pos' + (di.position.colour ? ` c-${di.position.colour}` : '');
        } else { info.pos.textContent = ''; info.pos.className = 'dm-pos'; }
        // classification circle
        info.flag.className = 'dm-flag' + (di.flag ? ` flag-${di.flag}` : '');
    }

    function setView(view) {
        state.view = view;
        document.querySelectorAll('#telemetryViewToggle .tile-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
        const tile = document.getElementById('telemetryTile');
        if (view === 'dashboard') {
            if (!_dashBuilt) buildDashboard(); else refreshDashDrivers();
            tile.classList.add('dashboard-mode');
            renderDashboard();
        } else {
            tile.classList.remove('dashboard-mode');
            resizeCanvas(); renderChart();   // canvas was hidden → re-measure + redraw
            renderCornerLabels();            // recompute label positions now the canvas has width (fixes all-left)
        }
    }

    // ── external selection API (standings / track map drive the dashboard) ──
    // window.F1Dashboard.focus(num): toggle that driver in the two gauge slots (card 277) —
    // select/unselect manually, same for every session; the clicked driver goes to pos2 and
    // promotes the current pos2 → pos1.
    let _dashOrder = [];   // running order [num,…] from the `standings` topic (selector order)
    messageBus.on('standings', (d) => {
        if (d && Array.isArray(d.drivers)) {
            _dashOrder = d.drivers.map(x => String(x.num));
            if (state.view === 'dashboard') paintDashSelector();   // keep selector in running order
            renderDriverSelector();   // telemetry rows follow current standings order (card)
        }
    });
    function dashSelect(nums) {
        const s = (nums || []).filter(Boolean).map(String).slice(0, 2);
        state.dashSlots = [s[0] || null, s[1] || null];
        setView('dashboard');   // populate + reveal the dashboard
        if (_dashBuilt) refreshDashDrivers();
    }
    // Server auto-select recommendation (card wfMzaSwh) — stored; applied (debounced) in renderDashboard.
    messageBus.on('dashAutoSelect', (pair) => { _autoPair = Array.isArray(pair) ? pair : []; autoTick(); });
    function dashFocus(num) { dashToggle(num); setView('dashboard'); }
    window.F1Dashboard = { focus: dashFocus, select: dashSelect };

    /**
     * Return drivers grouped by team, teams ordered by lowest driver number.
     * Within each team, lowest number first. Result: [{num, teamOrder}] where
     * teamOrder is 0 for the first team-mate and 1 for the second.
     */
    // Quali part number from a segment label ("Q2"/"SQ3" → 2/3); 0 if none.
    function partNum(seg) {
        const m = String(seg || '').match(/(\d)\s*$/);
        return m ? parseInt(m[1]) : 0;
    }

    // Card 71: in Q2/Q3 a driver eliminated in an earlier part is hidden from
    // the Last/Best views (their previous-part laps are no longer relevant).
    function koHidden(num) {
        return state.qualifyingPart >= 2 && state.eliminated.has(num);
    }

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

        // View toggle: trace chart ↔ live gauge dashboard.
        document.querySelectorAll('#telemetryViewToggle .tile-btn').forEach(btn => {
            btn.addEventListener('click', () => setView(btn.dataset.view));
        });

        // Auto-select toggle in the title bar (card wfMzaSwh).
        _autoBtn = document.getElementById('dashAutoToggle');
        if (_autoBtn) { _autoBtn.addEventListener('click', toggleAuto); syncAutoBtn(); }

        setupZoomInteractions();

        resizeCanvas();
        renderChart();
        renderCornerLabels();
        renderPartTabs();   // hidden until a qualifyingSegment arrives
        setView('dashboard');   // default to the live dashboard view (card 280)
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
            if (koHidden(num)) continue;   // card 71: no KO'd drivers in Q2/Q3
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
            renderDriverSelector();   // update the pill's selected (filled) state
        } else {
            state.hiddenDrivers.delete(driverNum);   // selecting a lap selects its driver (card)
            state.pendingSelection.add(key);
            messageBus.send({ cmd: 'getLapTelemetry', driver: driverNum, lap: lapIndex });
            renderDriverSelector();                  // reflect the now-selected driver chip
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

        // Mark the lap available the moment its telemetryLap row streams in
        // (card 69). These rows arrive during normal playback at the offset the
        // lap was committed — for an in-lap that's pit ENTRY (closed by the
        // server there), so its pill appears immediately instead of waiting for
        // the connect/seek telemetryAvailable refresh or the pit-out lap time.
        if (!isNaN(lap) && Array.isArray(data) && data.length > 0) {
            const set = state.telemetryLaps[driverNum] || (state.telemetryLaps[driverNum] = new Set());
            if (!set.has(lap)) { set.add(lap); renderDriverSelector(); }
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
        renderDriverSelector();   // reflect the selected (filled) pill state
    }

    // =========================================================================
    // Live Telemetry
    // =========================================================================

    // One server-decoded sample: {dp, speed, rpm, gear, throttle, brake,
    // ts, lap, lapElapsedMs}. dp is the track distance %. A change in `lap`
    // marks an S/F crossing → start a fresh live-lap trace. Samples with a
    // null dp (position outage) are skipped.
    function handleLiveTelemetry(num, data, offsetMs) {
        if (!data || typeof data !== 'object') return;
        if (data.dp == null) return;

        // New lap → reset the live trace so it shows the current lap only.
        if (state.liveLap[num] !== undefined && data.lap !== state.liveLap[num]) {
            state.liveSamples[num] = [];
        }
        state.liveLap[num] = data.lap;
        // Lap stopwatch: the absolute offset at this lap's S/F crossing = now − elapsed-since-S/F.
        if (data.lapElapsedMs != null) state.lapStartMs[num] = offsetMs - data.lapElapsedMs;

        const sample = [
            data.dp,
            data.speed || 0,
            data.rpm || 0,
            data.gear || 0,
            data.throttle || 0,
            data.brake || 0,
            offsetMs,          // [6] message offset (ms from session start) — for interpolation
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
        const margin = { top: 10, right: 10, bottom: 4, left: 45 };   // bottom trimmed — x-axis values removed (card 289)
        const plotW = w - margin.left - margin.right;
        const plotH = h - margin.top - margin.bottom;
        if (plotW <= 0 || plotH <= 0) return;

        const chInfo = CHANNELS[channel];
        const yMin = chInfo.min;
        const yMax = chInfo.max;

        drawGrid(ctx, margin, plotW, plotH, yMin, yMax, h, channel === 'speed' ? 60 : (yMax - yMin) / 5);
        // Yellow marshal-sector bands reflect the *current* track status, so
        // they only make sense against the live trace — the Best/Last/Selection
        // views show historical laps where a "now" yellow is meaningless.
        if (state.mode === 'live') drawYellowSectors(ctx, margin, plotW, plotH);

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
                const view = liveViewAt(samples, telRenderNowMs());
                drawTrace(ctx, view.trace, color, channel, margin, plotW, plotH, yMin, yMax, dashed);
                drawMarker(ctx, view.marker, color, tla,
                    channel, margin, plotW, plotH, yMin, yMax);
            }
        }
    }

    function drawGrid(ctx, margin, plotW, plotH, yMin, yMax, h, yStep) {
        const range = yMax - yMin || 1;
        yStep = yStep || range / 5;
        // Horizontal grid only (vertical x-grid removed, card 289) — lines + labels at each yStep.
        ctx.strokeStyle = 'rgba(255,255,255,0.08)';
        ctx.lineWidth = 0.5;
        for (let v = yMin; v <= yMax + 1e-6; v += yStep) {
            const y = margin.top + (1 - (v - yMin) / range) * plotH;
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(margin.left + plotW, y);
            ctx.stroke();
        }
        ctx.fillStyle = '#666';
        ctx.font = '10px Monaco, Consolas, monospace';
        ctx.textAlign = 'right';
        for (let v = yMin; v <= yMax + 1e-6; v += yStep) {
            const y = margin.top + (1 - (v - yMin) / range) * plotH;
            ctx.fillText(Math.round(v), margin.left - 4, y + 3);
        }
        // x-axis distance values removed (card 289) — only the corner numbers remain (HTML strip).

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
                const ls = state.liveSamples[num];
                samples = (ls && ls.length) ? liveViewAt(ls, telRenderNowMs()).trace : ls;
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
        let lastPct = null;    // previous sample's distance % — to detect lap wrap

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
            // Lap rollover: distance % resets ~100 → ~0 at the S/F line. Break the
            // trace here (pen up, NO bridge) so a live multi-lap trace doesn't
            // streak straight across the plot from ~100% back to ~0%. A single
            // completed lap climbs 0→100 monotonically, so this never fires there.
            // (eg7seHVk)
            const wrap = lastPct !== null && lastPct - s[0] > 50;
            if (wrap && inRun) {
                ctx.stroke();
                inRun = false;
            }
            if (!inRun) {
                // Bridge across an outage gap — but NOT across a lap wrap.
                if (lastValid && !wrap) {
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
            lastPct = s[0];
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
        // Rows in current standings order (falls back to team/car-number order); teamOrder (0/1)
        // still marks each team's 2nd car for the dashed-trace cue. (card)
        const teamOrderMap = {};
        for (const { num, teamOrder: to } of getSortedDrivers()) teamOrderMap[num] = to;
        const seen = new Set();
        const sorted = [];
        for (const num of _dashOrder) {
            if (state.drivers[num] && !seen.has(num)) { seen.add(num); sorted.push({ num, teamOrder: teamOrderMap[num] || 0 }); }
        }
        for (const { num } of getSortedDrivers()) {
            if (!seen.has(num)) { seen.add(num); sorted.push({ num, teamOrder: teamOrderMap[num] || 0 }); }
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

        // Qualifying pill tabs (card 66): show only the ACTIVE part's laps,
        // numbered locally (1,2,3…) so each driver's part laps left-align under
        // each other. Absolute lap numbers differ across drivers (out-lap counts
        // vary) — that's expected. hasSegments is false outside quali → flat
        // absolute-lap layout as before.
        const hasSegments = Object.values(state.lapSegments)
            .some(segMap => segMap && Object.values(segMap).some(s => s > 0));
        const activePart = state.activePart || state.qualifyingPart || 1;
        const localPos = {};   // num -> {absoluteLap -> localPos} within activePart
        let partMax = 0;
        if (hasSegments) {
            for (const num of Object.keys(state.drivers)) {
                const segs = state.lapSegments[num] || {};
                // Candidate laps = EVERY lap we know about (part-tagged + timed +
                // classified + on-disk telemetry). Out-laps and untimed in-laps
                // have no lap-time → no `part` tag, so without this they'd never
                // get a pill even though their telemetry exists. Infer a missing
                // lap's part from the nearest part-tagged lap.
                const known = Object.keys(segs).map(Number).filter(n => !isNaN(n)).sort((a, b) => a - b);
                const segOf = (lap) => {
                    if (segs[lap]) return segs[lap];
                    if (!known.length) return 0;
                    let best = known[0];
                    for (const k of known) if (Math.abs(k - lap) < Math.abs(best - lap)) best = k;
                    return segs[best];
                };
                const cand = new Set(known);
                for (const k of Object.keys(state.lapTimes[num] || {})) cand.add(parseInt(k));
                for (const k of Object.keys(state.lapCls[num] || {})) cand.add(parseInt(k));
                const tl = state.telemetryLaps[num];
                if (tl) for (const n of tl) cand.add(n);
                const partLaps = Array.from(cand)
                    .filter(l => !isNaN(l) && segOf(l) === activePart)
                    .sort((a, b) => a - b);
                localPos[num] = {};
                partLaps.forEach((absLap, idx) => { localPos[num][absLap] = idx + 1; });
                if (partLaps.length > partMax) partMax = partLaps.length;
            }
        }

        // Grid width: the active part's lap count in quali, else absolute maxLap.
        const totalLapCols = hasSegments ? Math.max(1, partMax) : maxLap;

        let html = `<div class="telemetry-driver-list" style="--max-lap:${totalLapCols}">`;
        for (const { num, teamOrder } of sorted) {
            const d = state.drivers[num];
            const hidden = state.hiddenDrivers.has(num) ? ' hidden' : '';
            const second = teamOrder === 1 ? ' second' : '';
            html += `<div class="telemetry-driver-entry${hidden}${second}" data-driver="${num}" style="--swatch-color:${d.color};--swatch-ink:${getContrastColor(d.color)}">` +
                    `<span class="telemetry-driver-row" data-action="toggle">` +
                    `<span class="telemetry-driver-tla">${d.tla}</span>` +
                    `</span>` +
                    renderLapList(num, maxLap, { hasSegments, activePart, localPos: localPos[num] || {} }) +
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
                if (wasHidden && (state.mode === 'last' || state.mode === 'best') && !koHidden(num)) {
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
        // once in init(); only its `.active` class is toggled here (shared .tile-btn).
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
        // Qualifying: render ONLY the active part's laps (card 66), placed at
        // local columns (TLA = col 1, first part-lap = col 2, …). localPos holds
        // just this driver's active-part laps.
        const useSegLayout = !!(segCtx && segCtx.hasSegments);
        const localPos = segCtx ? (segCtx.localPos || {}) : {};

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

        // In-progress lap = the highest lap still being DRIVEN — no lap_time and
        // not yet a completed lap. Don't render a pill for it (can't be selected
        // mid-lap); the current lap is only viewable in the Live view. An
        // IN/OUT/STOP lap is complete the moment the driver pits/stops (its
        // telemetry is committed there) even though F1 reports its lap-time only
        // at the next out-lap — so those, and any lap with committed telemetry,
        // are NOT in-progress and DO get a (grey, selectable) pill.
        let inProgressLap = null;
        for (const lap of allLaps) {
            if (lap in times) continue;                       // has a lap-time → complete
            const st = cls[lap];
            if (st === 'PIT' || st === 'STOP' || st === 'OUT') continue;  // completed in/out/stop lap
            if (teleLaps && teleLaps.has(lap)) continue;      // committed telemetry → selectable
            if (inProgressLap === null || lap > inProgressLap) {
                inProgressLap = lap;
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
                const lp = localPos[lap];
                if (!lp) continue;          // not in the active part → not in this tab
                col = lp + 1;               // local column (TLA is col 1)
                pillLabel = lap;            // absolute lap number (varies across drivers — expected)
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
            const selCls = state.selectionLaps[`${num}:${lap}`] ? ' selected' : '';
            html += `<span class="telemetry-lap-pill ${colorCls}${bestTypeCls}${selCls}"`
                  + ` data-driver="${num}" data-lap="${lap}" title="L${lap} ${times[lap] || ''}"`
                  + ` style="grid-column:${col}">${pillLabel}</span>`;
        }
        return html;
    }

    // Qualifying lap-part selector (cards 66/83): a single button showing the
    // Q1/Q2/Q3 (or SQ1-3): three always-visible buttons. A part that hasn't started yet is
    // disabled (Q2/Q3 during Q1, Q3 during Q2). The current part auto-selects when it starts
    // (state.activePart, set on qualifyingSegment). Hidden outside quali.
    function renderPartTabs() {
        const el = document.getElementById('telemetryPartTabs');
        if (!el) return;
        const part = state.qualifyingPart || 0;
        if (part <= 0) { el.classList.add('hidden'); el.innerHTML = ''; return; }
        el.classList.remove('hidden');
        const prefix = state.isSprintQuali ? 'SQ' : 'Q';
        const active = state.activePart || part;
        el.innerHTML = [1, 2, 3].map(p =>
            `<button class="telemetry-part${p === active ? ' active' : ''}" data-part="${p}"`
            + `${p > part ? ' disabled' : ''}>${prefix}${p}</button>`).join('');
        el.querySelectorAll('.telemetry-part').forEach(btn => btn.addEventListener('click', () => {
            const p = +btn.dataset.part;
            if (p > (state.qualifyingPart || 0)) return;   // future part → not selectable
            state.activePart = p;
            renderPartTabs();
            renderDriverSelector();
        }));
    }

    // =========================================================================
    // Legend (right-side overlay showing selected laps)
    // =========================================================================

    function updateDriverBar() {
        const legend = document.getElementById('telemetryLegend');
        if (!legend) return;

        let entries = Object.entries(currentLaps());
        // Selection-only legend: shows the laps currently overlaid on
        // the chart. Hidden whenever the selection is empty OR the
        // chart isn't in selection-rendering mode (last/best/selection).
        if (entries.length === 0 || state.mode === 'live') {
            legend.innerHTML = '';
            legend.classList.remove('has-entries');
            return;
        }

        // Last/Best: order the legend by lap time, fastest on top (card 72).
        // Re-runs whenever updateDriverBar does (i.e. each time a driver's last/
        // best lap is replaced). Entries with no usable time (IN/OUT/NO DATA)
        // sort to the bottom; Selection keeps the user's insertion order.
        if (state.mode === 'last' || state.mode === 'best') {
            entries = entries.slice().sort(([, a], [, b]) => {
                const ta = lapTimeToMs((state.lapTimes[a.driver] || {})[a.lap]);
                const tb = lapTimeToMs((state.lapTimes[b.driver] || {})[b.lap]);
                if (ta == null && tb == null) return 0;
                if (ta == null) return 1;
                if (tb == null) return -1;
                return ta - tb;
            });
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
                renderDriverSelector();   // clear the pill's selected (filled) state
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

    // Seek-safe raceStarted (gates the race lap pills): sessionStatus==='Started'
    // is edge-triggered and not reconstructable on seek. raceLaps.currentLap IS
    // restored latest-per-topic and stays >= 1 from lights-out through the
    // chequered flag, so deriving from it means pills don't vanish past chequered
    // nor show pre-race. Cleared on state:reset, re-derived here on restore. (Prit9naE)
    messageBus.on('raceLaps', (d) => {
        if (d && d.currentLap >= 1 && !state.raceStarted) {
            state.raceStarted = true;
            renderDriverSelector();
        }
    });

    // Position-data warning (above the x-axis), driven by the server's dataHealth:
    // yellow when the GPS OR telemetry feed is down; red when BOTH are down.
    function updatePosWarning(health) {
        const el = document.getElementById('telePosWarning');
        const msg = document.getElementById('telePosWarningMsg');
        if (!el || !msg) return;
        const posRed = !!(health && health.position && health.position.level === 'red');
        const telRed = !!(health && health.telemetry && health.telemetry.level === 'red');
        if (posRed && telRed) {
            el.classList.add('red');
            msg.textContent = 'Telemetry and position data unavailable. Data is unreliable';
            el.hidden = false;
        } else if (posRed || telRed) {
            el.classList.remove('red');
            msg.textContent = 'Position data unavailable. Track position estimated from telemetry';
            el.hidden = false;
        } else {
            el.hidden = true;
        }
    }
    messageBus.on('dataHealth', updatePosWarning);
    messageBus.on('state:reset', () => updatePosWarning(null));

    messageBus.on('driverList', (data) => {
        if (!data || typeof data !== 'object') return;
        for (const [num, info] of Object.entries(data)) {
            const isNew = !state.drivers[num];
            state.drivers[num] = {
                tla: info.tla || num,
                color: info.teamColour ? `#${info.teamColour}` : (TEAM_COLORS[num] || DEFAULT_CAR_COLOR),
                teamName: info.teamName || '',
            };
            if (isNew) state.hiddenDrivers.add(num);   // default: unselected (no trace until picked) (card)
        }
        renderDriverSelector();
        refreshDashDrivers();   // dashboard ticker + slots follow the driver list
    });

    // Live trace: the server now emits a fully-decoded per-driver sample
    // (dp = track %, channels, lap) — no more client-side CarData.z decode
    // or position-derived distance. One sample per emit.
    messageBus.on('liveTelemetry:', (topic, data, offsetMs) => {
        const num = topic.split(':')[1];
        if (num) handleLiveTelemetry(num, data, offsetMs);
    });

    messageBus.on('driverStatus:', (topic, data) => {
        const num = topic.split(':')[1];
        if (num) handleDriverStatus(num, data);
    });

    // Qualifying part + knockouts (cards 66/71). segment "Q1".."Q3"/"SQ1"..
    // drives the pill tabs; eliminated drives the Best/Last KO filter.
    messageBus.on('qualifyingSegment', (data) => {
        if (!data) return;
        const newPart = partNum(data.segment);
        const partChanged = newPart && newPart !== state.qualifyingPart;
        if (newPart) state.qualifyingPart = newPart;
        // A new part starts on its own tab (card 83); within a part the user's
        // selection is preserved (qualifyingSegment also fires on elimination).
        if (partChanged) state.activePart = newPart;
        state.eliminated = new Set((data.eliminated || []).map(String));
        state.isSprintQuali = !!data.isSprintQuali;
        // Drop eliminated drivers' Last/Best laps so they leave those views.
        for (const map of [state.lastLaps, state.bestLaps]) {
            for (const k of Object.keys(map)) {
                if (koHidden(map[k].driver)) delete map[k];
            }
        }
        renderPartTabs();
        renderDriverSelector();
        updateDriverBar();
        scheduleRender();
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
            // Per-lap quali part → drives the pill tabs (card 66).
            if (data.lastLap.part != null) {
                (state.lapSegments[num] || (state.lapSegments[num] = {}))[data.lastLap.lap] =
                    data.lastLap.part;
            }
        }
        // Per-driver fastest lap → purple pill (server flags bestLap from
        // PersonalFastest/OverallFastest, so it excludes out/in/cool laps).
        const prevBest = state.bestLapNum[num];
        if (data.bestLap && data.bestLap.lap != null) {
            state.bestLapNum[num] = data.bestLap.lap;
        }
        // Last view: refresh this driver's lap when it completes a newer one.
        if (state.mode === 'last' && data.lastLap && data.lastLap.lap != null
                && !state.hiddenDrivers.has(num) && !koHidden(num)
                && state.lastSeenLastLap[num] !== data.lastLap.lap) {
            state.lastSeenLastLap[num] = data.lastLap.lap;
            state.pendingLast.add(num);
            messageBus.send({ cmd: 'getLastLapTelemetry', driver: num });
        }
        // Best view: refresh this driver's lap when its best improves.
        if (state.mode === 'best' && data.bestLap && data.bestLap.lap != null
                && !state.hiddenDrivers.has(num) && !koHidden(num)
                && data.bestLap.lap !== prevBest) {
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

    // dashInfo:{num} — server-computed dashboard info-tile state (P/Q): stopwatch running/frozen,
    // delta/result, position, classification circle. Render-only (card 277).
    messageBus.on('dashInfo:', (topic, data) => {
        const num = topic.split(':')[1];
        if (num) state.dashInfo[num] = data || {};
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
        // Canvas hidden/unmeasured (e.g. dashboard view active) → plotW 0 would pctToX every corner
        // to leftMargin (all labels pile on the left). Bail; setView/resize recompute once it's shown.
        if (plotW <= 0) return;

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
        state.eliminated = new Set();   // rebuilt from the restored qualifyingSegment
        state.lapNoData = {};
        state.telemetryLaps = {};
        // Race-dashboard info-tile state + stopwatch S/F offsets are edge-
        // accumulated; clear them so a backward seek doesn't leave a stale
        // dashInfo panel or a stopwatch running from a "future" lap. (pJLP0F6W)
        state.dashInfo = {};
        state.lapStartMs = {};
        // Marshal-sector yellow bands are edge-accumulated from yellowFlag; clear
        // so a seek to before the first status doesn't leave a stale yellow band
        // (the restore re-emits the correct yellowFlag at the target). (MXck3xpg)
        state.yellowSectors = [];
        // raceStarted gates the race lap pills; it's edge-set from sessionStatus,
        // so clear it and let the restored raceLaps.currentLap re-derive it durably
        // (correct at any offset, incl. past chequered). (Prit9naE)
        state.raceStarted = false;
        // Last/Best are view snapshots — clear on reset. Selection is the
        // user's set and SURVIVES seek/restore; "future" laps are pruned on
        // state:restore-done below (backward seek only), once the lap history has
        // been rebuilt.
        state.lastLaps = {};
        state.bestLaps = {};
        state.pendingSelection.clear();
        state.pendingLast.clear();
        state.pendingBest.clear();
        state.lastSeenLastLap = {};
        renderDriverSelector();
    });

    // After a seek, drop Selection laps that no longer exist at the new clock
    // (backward seek → "future" laps). This runs on state:restore-done, NOT the
    // earlier state:seek-complete: lapTimes/telemetryLaps are wiped on state:reset
    // and only rebuilt by the driverLaps: history + telemetryAvailable extras,
    // which stream in AFTER seek-complete. Pruning on seek-complete therefore saw
    // near-empty maps and wiped the user's whole selection — even on a forward
    // seek. At restore-done the maps are complete, so valid laps survive and only
    // genuine future laps (backward seek) drop. Paint via the shared gate so it
    // lands in the single restore-done flush. (SOJffVd3)
    messageBus.on('state:restore-done', () => {
        for (const k of Object.keys(state.selectionLaps)) {
            const { driver, lap } = state.selectionLaps[k];
            const known = (state.lapTimes[driver] && lap in state.lapTimes[driver])
                || (state.telemetryLaps[driver] && state.telemetryLaps[driver].has(lap));
            if (!known) delete state.selectionLaps[k];
        }
        messageBus.scheduleRender('telemetry', () => { renderChart(); updateDriverBar(); });
    });

    document.addEventListener('DOMContentLoaded', init);

})();
