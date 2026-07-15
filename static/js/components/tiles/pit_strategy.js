/**
 * Pit-strategy tabs — injected into the race-control tile's tab bar (reuses its
 * generic #rcTabs / .rc-pane switching). Two tabs:
 *   - Pit stops : every observed in-race pit stop + measured loss (race only).
 *                 Renders the `pitStopTimeLoss.stops` list (server-computed).
 *   - Strategy  : the pre-race pit-lane TRANSIT prediction (fetched from
 *                 /analysis/pit_loss_estimate) + the running observed loss
 *                 (green, and derived VSC 0.6× / SC 0.4×) from `pitStopTimeLoss`.
 * Presentation is intentionally minimal — iterate later. Server computes, we render.
 */
(function () {
    let stops = [];
    let observed = null;
    let transit = null;

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[c]);
    }
    function s1(v) { return (typeof v === 'number') ? v.toFixed(1) : '—'; }

    // Track-clock (hh:MM:ss) from an InPit UTC ISO string + the circuit GMT offset.
    let _gmtMs = 0;
    function parseGmt(str) {
        const m = String(str || '').match(/^(-?)(\d+):(\d+):(\d+)$/);
        if (!m) return 0;
        return (m[1] === '-' ? -1 : 1) * (parseInt(m[2]) * 3600 + parseInt(m[3]) * 60) * 1000;
    }
    function toClock(iso) {
        if (!iso) return '';
        let s = String(iso);
        if (!/[Zz]$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
        const d = new Date(s);
        return isNaN(d.getTime()) ? '' : new Date(d.getTime() + _gmtMs).toUTCString().slice(17, 25);
    }
    // SC/VSC take priority over green (server-computed over the whole pit transit).
    function statusCell(cls) {
        if (cls === 'sc') return '<span class="pit-badge pit-sc">SC</span>';
        if (cls === 'vsc') return '<span class="pit-badge pit-vsc">VSC</span>';
        return '<span class="pit-flag" title="Green"></span>';
    }
    // Traffic on rejoin, from the interval to the car ahead: <3s TRAFFIC (red), 3–5s TRAFFIC
    // (yellow), >5s nothing. Only flag when the car ahead has done FEWER stops (it's yet to pit —
    // real traffic); if it's on the same/more stops it's just settled order. A non-numeric interval
    // (null = lapped) can't be compared → no flag. (UNDERCUT/OVERCUT are yet to be defined.)
    function trafficCell(s) {
        if (typeof s.aheadStops !== 'number' || s.aheadStops >= s.stopNumber) return '';
        const iv = s.intAfter_s;
        if (typeof iv !== 'number') return '';
        if (iv < 3) return '<span class="pit-traffic tr-red">TRAFFIC</span>';
        if (iv <= 5) return '<span class="pit-traffic tr-yellow">TRAFFIC</span>';
        return '';
    }
    // Tyre fitted at the stop — compound icon only (always a fresh set).
    function tyreImg(compound) {
        if (!compound) return '';
        const c = String(compound).toLowerCase();
        return `<img class="pit-tyre" src="/static/images/tyres/${c}-new.svg" alt="${escapeHtml(compound)}">`;
    }
    // Stop-time colour: fastest overall = purple; else <3 green, <4 yellow, <6 orange, else red.
    function stopClass(v, fastest) {
        if (typeof v !== 'number') return '';
        if (v === fastest) return 'st-purple';
        if (v < 3) return 'st-green';
        if (v < 4) return 'st-yellow';
        if (v < 6) return 'st-orange';
        return 'st-red';
    }

    // ── inject the two tabs + panes into the race-control tile ──────────────
    function injectTabs() {
        const t = (window.SESSION_CONFIG || {}).sessionType || '';
        if (t !== 'race' && t !== 'sprint') return false;   // Pit stops/Strategy: race-style only (card)
        const tabs = document.getElementById('rcTabs');
        if (!tabs || document.getElementById('pitPaneStops')) return false;
        const content = tabs.closest('.tile') &&
            tabs.closest('.tile').querySelector('.tile-content');
        if (!content) return false;

        const mkBtn = (tab, label) => {
            const b = document.createElement('button');
            b.className = 'tile-btn';
            b.dataset.tab = tab;
            b.textContent = label;
            return b;
        };
        const mkPane = (pane, id) => {
            const d = document.createElement('div');
            d.className = 'rc-pane pit-pane';
            d.dataset.pane = pane;
            d.id = id;
            return d;
        };
        tabs.appendChild(mkBtn('pitstops', 'Pit stops'));
        content.appendChild(mkPane('pitstops', 'pitPaneStops'));
        // Strategy tab not shipped yet (v2.0.0) — omitted until it's finished.
        return true;
    }

    // ── rendering ───────────────────────────────────────────────────────────
    // Pit stops tab: the predicted pit-lane transit (moved here) + per-stop observed
    // pit-lane duration, stationary time, and total time lost.
    function renderStops() {
        const el = document.getElementById('pitPaneStops');
        if (!el) return;
        let html = '';
        const item = (value, label) =>
            `<span class="pit-strat-item"><span class="pit-strat-big">${value}</span>` +
            `<span class="pit-strat-title">${label}</span></span>`;
        const parts = [];
        if (transit) parts.push(item(`${s1(transit.pit_lane_transit_s)}s`, 'Predicted'));
        if (observed && typeof observed.transit_s === 'number') {
            parts.push(item(`${s1(observed.transit_s)} ± ${s1(observed.transit_std_s)}s`, 'Measured'));
        }
        if (parts.length) html += `<div class="pit-strat-block">` +
            `<span class="pit-strat-label">Pit-lane transit</span>${parts.join('')}</div>`;
        if (!stops.length) {
            el.innerHTML = html + '<div class="rc-empty">No pit stops yet.</div>';
            return;
        }
        const fastest = Math.min.apply(null,
            stops.map((s) => typeof s.stationary_s === 'number' ? s.stationary_s : Infinity));
        // Most recent first — by the InPit event timestamp.
        const sorted = stops.slice().sort((a, b) =>
            (Date.parse(b.t_pit_utc) || 0) - (Date.parse(a.t_pit_utc) || 0));
        const head = `<div class="pit-row pit-head">` +
            `<span class="pit-time">Time</span><span class="pit-lap">Lap</span>` +
            `<span class="pit-drv">Driver</span><span class="pit-tyre-h"></span>` +
            `<span class="pit-c">#</span>` +
            `<span class="pit-status"></span><span class="pit-num">Stop</span>` +
            `<span class="pit-num">Time lost</span><span class="pit-pos">Pos</span>` +
            `<span class="pit-traffic-h">Traffic</span></div>`;
        const pos = (b, a) => `P${b ?? '?'} → P${a ?? '?'}`;
        const rows = sorted.map((s) => {
            const colour = (s.color || '').replace(/^#/, '');
            return `<div class="pit-row">` +
                `<span class="pit-time">${toClock(s.t_pit_utc)}</span>` +
                `<span class="pit-lap">L${s.lap ?? '—'}</span>` +
                `<span class="pit-drv"><span class="pit-dot" style="--team-colour:#${escapeHtml(colour || '888')}"></span>${escapeHtml(s.tla)}</span>` +
                `<span class="pit-tyre-c">${tyreImg(s.compound)}</span>` +
                `<span class="pit-c">${s.stopNumber ?? ''}</span>` +
                `<span class="pit-status">${statusCell(s.cls)}</span>` +
                `<span class="pit-num ${stopClass(s.stationary_s, fastest)}" title="stationary time">${s1(s.stationary_s)}s</span>` +
                `<span class="pit-num" title="total time lost (gap before vs after)">${typeof s.timeLost_s === 'number' ? s.timeLost_s.toFixed(1) + 's' : ''}</span>` +
                `<span class="pit-pos">${pos(s.posBefore, s.posAfter)}</span>` +
                `<span class="pit-traffic-c" title="gap to car ahead on rejoin">${trafficCell(s)}</span>` +
                `</div>`;
        }).join('');
        el.innerHTML = html + head + rows;
    }

    // Strategy tab: intentionally empty for now (future: drop-position prediction).
    function renderStrategy() {
        const el = document.getElementById('pitPaneStrategy');
        if (el) el.innerHTML = '';
    }

    function renderAll() { renderStops(); renderStrategy(); }

    // ── data ────────────────────────────────────────────────────────────────
    async function fetchTransit() {
        const sessionId = (window.SESSION_CONFIG || {}).sessionId
            || (window.SESSION_CONFIG || {}).sessionKey;
        if (!sessionId) return;
        try {
            const resp = await fetch(
                `/api/v1/livetiming/analysis/pit_loss_estimate/${encodeURIComponent(sessionId)}`);
            if (!resp.ok) return;
            transit = await resp.json();
            renderStops();
        } catch (e) { /* transit block just stays hidden */ }
    }

    messageBus.on('pitStopTimeLoss', (data) => {
        if (!data || typeof data !== 'object') return;
        stops = Array.isArray(data.stops) ? data.stops : [];
        observed = data.observed || null;
        renderAll();
    });

    messageBus.on('sessionInfo', (data) => {
        if (data && data.gmtOffset) { _gmtMs = parseGmt(data.gmtOffset); renderStops(); }
    });
    messageBus.on('state:reset', () => { stops = []; observed = null; renderAll(); });

    function init() {
        if (!injectTabs()) return;
        renderAll();
        fetchTransit();
    }
    document.addEventListener('DOMContentLoaded', init);
    if (document.readyState !== 'loading') init();
})();
