/**
 * Race-control tile — three tabs:
 *   - Race control  : F1 RC messages — accumulates the `raceControlMessage`
 *                     topic (one per message; server replays history on
 *                     connect/seek).
 *   - Pecking order : team-rank prediction; fetched from the prior session's
 *                     pecking_order.json via /api/v1/livetiming/analysis/...
 *   - Championship  : drivers + constructors standings; updates from the
 *                     championshipDrivers / championshipConstructors topics
 *                     during a race. Hidden outside race/sprint sessions.
 */

(function() {
    let peckingHtml = '';
    let rcMessages = [];
    let champDrivers = [];        // server-computed, fully self-contained rows
    let champConstructors = [];
    let radioClips = [];          // team radio (card 8): {num, tla, file, utc}
    const _radioSeen = new Set();

    // SVG sizing/colour comes from CSS (no presentational attrs on the markup).
    const RADIO_PLAY_SVG = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
    const RADIO_STOP_SVG = '<svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>';
    const RADIO_ICON_SVG = '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>';

    // Epoch ms from an F1 Utc string (RCM ships without a 'Z'; radio with one).
    function _epochMs(ts) {
        if (!ts) return 0;
        let s = String(ts);
        if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
        const t = Date.parse(s);
        return isNaN(t) ? 0 : t;
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[c]);
    }

    // Track GMT offset (ms), captured from SessionInfo below — see the
    // sessionInfo handler. Owned here rather than read from messageBus so RCM
    // timestamps don't depend on the header having set it first (card 86).
    let _trackGmtOffsetMs = 0;
    function parseGmtOffsetMs(str) {
        if (!str) return 0;
        const m = String(str).match(/^(-?)(\d+):(\d+):(\d+)$/);
        if (!m) return 0;
        const sign = m[1] === '-' ? -1 : 1;
        return sign * (parseInt(m[2]) * 3600 + parseInt(m[3]) * 60) * 1000;
    }

    function toLocalTimeStr(timestamp) {
        if (!timestamp) return '';
        try {
            // The F1 RCM `Utc` field is UTC but ships without a 'Z'/offset
            // suffix; new Date() would then parse it in the BROWSER's local
            // zone. When the viewer's zone equals the track's, the local-parse
            // shift and the GMT-offset addition below cancel, leaving the raw
            // UTC value on screen (card 95-style). Force UTC by appending 'Z'
            // when no timezone designator is present.
            let iso;
            if (timestamp.includes('T')) {
                const timePart = timestamp.slice(timestamp.indexOf('T') + 1);
                const hasTz = /[Zz]$|[+-]\d\d:?\d\d$/.test(timePart);
                iso = hasTz ? timestamp : `${timestamp}Z`;
            } else {
                iso = `2000-01-01T${timestamp}Z`;
            }
            const utc = new Date(iso);
            // Track time (matches the header track clock): UTC + the circuit's
            // GMT offset (card 86). A future track/user toggle switches the
            // source for all visible times.
            return new Date(utc.getTime() + _trackGmtOffsetMs).toUTCString().slice(17, 25);
        } catch (e) {
            const t = timestamp.includes('T') ? timestamp.split('T')[1] : timestamp;
            return t.slice(0, 8);
        }
    }

    // ── Pecking order ──────────────────────────────────────────────

    function buildPeckingHtml(entries, kind) {
        // entries are pecking_order.json shape:
        //   { rank, team, color, gap_s, cohort, confidence, weight }
        if (!entries || !entries.length) return '';
        let html = `<div class="race-control-msg pecking-header">` +
            `<span class="race-control-time"></span>` +
            `<span class="race-control-text">${kind} pace · predicted</span>` +
            `</div>`;
        for (const e of entries) {
            const colour = (e.color || '').replace(/^#/, '');
            const colourStyle = colour ? ` style="--team-colour:#${escapeHtml(colour)}"` : '';
            const gap = (typeof e.gap_s === 'number')
                ? (e.gap_s === 0 ? '' : `+${e.gap_s.toFixed(1)}s`) : '';
            const conf = typeof e.confidence === 'number' ? e.confidence : 0;
            const confOpacity = (0.3 + 0.7 * Math.max(0, Math.min(1, conf))).toFixed(2);
            html += `<div class="race-control-msg pecking-row">` +
                `<span class="race-control-time">${e.rank}</span>` +
                `<span class="race-control-text">` +
                    `<span class="pecking-team-colour"${colourStyle}></span>` +
                    `<span class="pecking-team">${escapeHtml(e.team)}</span>` +
                    `<span class="pecking-gap">${gap}</span>` +
                    `<span class="pecking-conf" title="Confidence ${(conf*100).toFixed(0)}%" style="opacity:${confOpacity}">●</span>` +
                `</span></div>`;
        }
        return html;
    }

    async function fetchPriorPeckingOrder() {
        const sessionId = (window.SESSION_CONFIG || {}).sessionId
            || (window.SESSION_CONFIG || {}).sessionKey;
        if (!sessionId) return;
        try {
            const resp = await fetch(
                `/api/v1/livetiming/analysis/pecking_order/${encodeURIComponent(sessionId)}`,
            );
            if (!resp.ok) return;
            const payload = await resp.json();
            const sessionType = window.SESSION_CONFIG?.sessionType || '';
            let entries = null;
            let kind = null;
            if (sessionType === 'race' || sessionType === 'sprint') {
                entries = payload.race_pecking_order; kind = 'Race';
            } else {
                // Practice / Qualifying / SQ: prefer quali, fall back to race.
                entries = payload.quali_pecking_order || payload.race_pecking_order;
                kind = payload.quali_pecking_order ? 'Qualifying' : 'Race';
            }
            peckingHtml = buildPeckingHtml(entries, kind);
            renderAll();
        } catch (e) {
            /* swallow — pane stays at "Loading…" then "—" */
        }
    }

    // ── Championship ───────────────────────────────────────────────

    function isRaceSession() {
        const t = window.SESSION_CONFIG?.sessionType || '';
        return t === 'race' || t === 'sprint';
    }

    // Pecking order is only meaningful from Q onwards. Hide the tab
    // entirely during practice — FP1 pecking-order is computed at end
    // of session, showing the prior session's during FP would be a
    // mid-session distraction (= per SME 2026-06-07).
    function isPeckingSession() {
        const t = window.SESSION_CONFIG?.sessionType || '';
        return t !== 'practice' && !isRaceSession();   // quali-only: hidden in practice AND race/sprint (card)
    }

    function buildChampHtml(drivers, constructors) {
        if (!drivers.length && !constructors.length) return '';

        function colourBar(hex) {
            const c = (hex || '').replace(/^#/, '');
            return c
                ? `<span class="pecking-team-colour" style="--team-colour:#${escapeHtml(c)}"></span>`
                : `<span class="pecking-team-colour"></span>`;
        }
        function fmtPts(n) {
            if (n == null) return '';
            return (n === Math.trunc(n)) ? String(Math.trunc(n)) : String(n);
        }
        // Position change: green ▲ for places gained, red ▼ for lost,
        // empty when unchanged.
        function changeHtml(chg) {
            if (!chg) return `<span class="rc-champ-change"></span>`;
            const up = chg > 0;
            return `<span class="rc-champ-change ${up ? 'rc-champ-up' : 'rc-champ-down'}">`
                + `${up ? '▲' : '▼'}${Math.abs(chg)}</span>`;
        }
        // Layout: colour | name | points-today | projected-total | ▲/▼ N.
        // All fields are server-computed (championship_processor): teamColour,
        // name, predictedPoints, pointsGained (today), positionsGained.
        function rowHtml(rank, colourHex, name, row) {
            const today = row.pointsGained > 0 ? `+${fmtPts(row.pointsGained)}` : '';
            return `<div class="rc-champ-row">` +
                `<span class="rc-champ-rank">${rank}</span>` +
                colourBar(colourHex) +
                `<span class="rc-champ-name">${escapeHtml(name)}</span>` +
                `<span class="rc-champ-today">${today}</span>` +
                `<span class="rc-champ-pts">${fmtPts(row.predictedPoints)}</span>` +
                changeHtml(row.positionsGained) +
                `</div>`;
        }

        // ── Drivers column ────────────────────────────────────────
        let leftHtml = `<div class="rc-champ-col-title">Drivers</div>`;
        drivers.forEach((row, i) => {
            const name = row.driverName || `#${row.driverNumber}`;
            leftHtml += rowHtml(i + 1, row.teamColour, name, row);
        });

        // ── Constructors column ───────────────────────────────────
        let rightHtml = `<div class="rc-champ-col-title">Constructors</div>`;
        constructors.forEach((row, i) => {
            if (row.teamName == null) return;
            rightHtml += rowHtml(i + 1, row.teamColour, row.teamName, row);
        });

        return `<div class="rc-champ-cols">` +
                 `<div class="rc-champ-col">${leftHtml}</div>` +
                 `<div class="rc-champ-col">${rightHtml}</div>` +
               `</div>`;
    }

    // ── Rendering ──────────────────────────────────────────────────

    // One message per emit; the server replays history on connect/seek
    // (after a state:reset), so we just accumulate.
    function handleMessage(data) {
        if (!data || typeof data !== 'object' || !data.message) return;
        rcMessages.push(data);
        renderAll();
    }

    function rcmRow(msg) {
        // Colour is fully server-computed (race_control_processor); the client
        // just maps the colour name to its CSS class.
        let colorClass = '';
        if (msg.color === 'yellow') colorClass = 'rc-yellow';
        else if (msg.color === 'red') colorClass = 'rc-red';
        else if (msg.color === 'green') colorClass = 'rc-green';
        else if (msg.color === 'blue') colorClass = 'rc-blue';
        else if (msg.color === 'chequered') colorClass = 'rc-chequered';
        else if (msg.color === 'orange') colorClass = 'rc-orange';
        return `<div class="race-control-msg ${colorClass}">` +
            `<span class="race-control-time">${toLocalTimeStr(msg.timestamp)}</span>` +
            `<span class="race-control-text">${escapeHtml(msg.message)}</span>` +
            `</div>`;
    }

    function radioRow(clip) {
        const who = escapeHtml(clip.tla || clip.num || '');
        const f = escapeHtml(clip.file);
        // Order: time · audio icon · driver TLA · "Team radio" · play · stop.
        // Timestamp matches the RCM rows (clip broadcast Utc → track-local). (938qwRAp)
        return `<div class="race-control-msg rc-radio">` +
            `<span class="race-control-time">${toLocalTimeStr(clip.utc)}</span>` +
            `<span class="rc-radio-icon">${RADIO_ICON_SVG}</span>` +
            `<span class="rc-radio-tla">${who}</span>` +
            `<span class="race-control-text">Team radio</span>` +
            `<button class="rc-radio-play" data-radio="${f}" title="Play team radio" aria-label="Play team radio">${RADIO_PLAY_SVG}</button>` +
            `<button class="rc-radio-stop" data-radio="${f}" title="Stop team radio" aria-label="Stop team radio">${RADIO_STOP_SVG}</button>` +
            `</div>`;
    }

    function renderAll() {
        const rcm = document.getElementById('rcPaneRcm');
        const radio = document.getElementById('rcPaneRadio');
        const peck = document.getElementById('rcPanePecking');
        const champ = document.getElementById('rcPaneChamp');
        if (!rcm) return;

        // RCM pane: race-control messages + team-radio clips interleaved by time.
        const merged = [];
        for (const msg of rcMessages) merged.push({ t: _epochMs(msg.timestamp), html: rcmRow(msg) });
        for (const clip of radioClips) merged.push({ t: _epochMs(clip.utc), html: radioRow(clip) });
        merged.sort((a, b) => a.t - b.t);
        rcm.innerHTML = merged.map(m => m.html).join('') || '<div class="rc-empty">No messages yet.</div>';
        rcm.scrollTop = rcm.scrollHeight;

        if (radio) {
            radio.innerHTML = radioClips.map(radioRow).join('')
                || '<div class="rc-empty">No team radio yet.</div>';
        }
        if (peck) {
            peck.innerHTML = peckingHtml
                || '<div class="rc-empty">Pecking order will appear after FP1 ends.</div>';
        }
        if (champ) {
            champ.innerHTML = buildChampHtml(champDrivers, champConstructors)
                || '<div class="rc-empty">Championship standings appear once the race starts.</div>';
        }
    }

    function applyChampionshipTabVisibility() {
        // Championship tab only relevant in race-style sessions.
        const tab = document.querySelector('#rcTabs .tile-btn[data-tab="champ"]');
        const pane = document.getElementById('rcPaneChamp');
        const showChamp = isRaceSession();
        if (tab) tab.style.display = showChamp ? '' : 'none';
        if (pane && !showChamp) pane.classList.remove('active');
    }

    function applyPeckingTabVisibility() {
        const tab = document.querySelector('#rcTabs .tile-btn[data-tab="pecking"]');
        const pane = document.getElementById('rcPanePecking');
        const showPecking = isPeckingSession();
        if (tab) tab.style.display = showPecking ? '' : 'none';
        if (pane && !showPecking) pane.classList.remove('active');
    }

    function activateTab(tab) {
        document.querySelectorAll('#rcTabs .tile-btn').forEach((b) => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
        document.querySelectorAll('.rc-pane').forEach((p) => {
            p.classList.toggle('active', p.dataset.pane === tab);
        });
    }

    document.addEventListener('click', (e) => {
        // Team-radio play/stop buttons → delegate to the shared player (header.js).
        const play = e.target.closest('.rc-radio-play');
        if (play) {
            const file = play.dataset.radio;
            if (file && typeof window.playTeamRadio === 'function') window.playTeamRadio(file);
            return;
        }
        if (e.target.closest('.rc-radio-stop')) {
            if (typeof window.stopTeamRadio === 'function') window.stopTeamRadio();
            return;
        }
        const btn = e.target.closest('#rcTabs .tile-btn');
        if (!btn) return;
        activateTab(btn.dataset.tab);
        // Lazy fetch — guarantees the pane shows data even if the
        // initial DOMContentLoaded fetch fired before the pane existed.
        if (btn.dataset.tab === 'pecking' && !peckingHtml && isPeckingSession()) {
            fetchPriorPeckingOrder();
        }
    });

    // ── Wiring ─────────────────────────────────────────────────────

    messageBus.on('raceControlMessage', handleMessage);
    messageBus.on('championshipDrivers', (data) => {
        if (Array.isArray(data)) { champDrivers = data; renderAll(); }
    });
    messageBus.on('championshipConstructors', (data) => {
        if (Array.isArray(data)) { champConstructors = data; renderAll(); }
    });
    // Team radio (card 8): accumulate clips (deduped by file). The driver TLA is
    // the filename prefix ("LEC_16_…"). Replayed on connect/seek by the server.
    messageBus.on('teamRadio', (data) => {
        if (!data || !data.file || _radioSeen.has(data.file)) return;
        _radioSeen.add(data.file);
        radioClips.push({
            num: data.num, file: data.file, utc: data.utc || '',
            tla: (String(data.file).split('_')[0] || '').toUpperCase(),
        });
        radioClips.sort((a, b) => _epochMs(a.utc) - _epochMs(b.utc));
        renderAll();
    });
    // Capture the circuit GMT offset so RCM timestamps render in track time
    // (card 86), matching the header track clock; re-render once it arrives.
    messageBus.on('sessionInfo', (data) => {
        if (data && data.gmtOffset) _trackGmtOffsetMs = parseGmtOffsetMs(data.gmtOffset);
        renderAll();
    });

    messageBus.on('state:reset', () => {
        peckingHtml = '';
        rcMessages = [];
        champDrivers = [];
        champConstructors = [];
        radioClips = [];
        _radioSeen.clear();
        renderAll();
    });

    function init() {
        applyChampionshipTabVisibility();
        applyPeckingTabVisibility();
        if (isPeckingSession()) {
            fetchPriorPeckingOrder();
        }
    }
    document.addEventListener('DOMContentLoaded', init);
    // Some pages have already fired DOMContentLoaded by the time this
    // script runs — fall through immediately in that case.
    if (document.readyState !== 'loading') init();

})();
