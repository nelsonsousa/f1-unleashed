/**
 * Race-control tile — three tabs:
 *   - Race control  : F1 RC messages (= subscribes to raceControlMessages)
 *   - Pecking order : team-rank prediction; fetched from the prior session's
 *                     pecking_order.json via /api/v1/livetiming/analysis/...
 *   - Championship  : drivers + constructors standings; updates from the
 *                     championshipPrediction topic during a race. Hidden
 *                     entirely outside race/sprint sessions.
 */

(function() {
    let peckingHtml = '';
    let rcMessages = [];
    let champPayload = null;
    let driverList = {};   // num → {tla, teamColour, teamName}

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[c]);
    }

    function parseGmtOffsetMs(str) {
        if (!str) return 0;
        const m = str.match(/^(-?)(\d+):(\d+):(\d+)$/);
        if (!m) return 0;
        const sign = m[1] === '-' ? -1 : 1;
        return sign * (parseInt(m[2]) * 3600 + parseInt(m[3]) * 60) * 1000;
    }

    function toLocalTimeStr(timestamp) {
        if (!timestamp) return '';
        try {
            const utc = new Date(timestamp.includes('T') ? timestamp : `2000-01-01T${timestamp}Z`);
            const offsetMs = messageBus.gmtOffset ? parseGmtOffsetMs(messageBus.gmtOffset) : 0;
            return new Date(utc.getTime() + offsetMs).toUTCString().slice(17, 25);
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
        return t !== 'practice';
    }

    function buildChampHtml(payload) {
        if (!payload) return '';
        const drivers = payload.drivers || [];
        const constructors = payload.constructors || [];

        // Build a team→colour lookup from the cached driverList so
        // constructors can show their team colour bar too.
        const teamColours = {};
        for (const num of Object.keys(driverList)) {
            const info = driverList[num] || {};
            if (info.teamName && info.teamColour) {
                teamColours[info.teamName] = info.teamColour;
            }
        }

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
        // points-today is blank for drivers not expected to score today.
        function rowHtml(rank, colourHex, name, row) {
            const today = row.pointsToday > 0 ? `+${fmtPts(row.pointsToday)}` : '';
            return `<div class="rc-champ-row">` +
                `<span class="rc-champ-rank">${rank}</span>` +
                colourBar(colourHex) +
                `<span class="rc-champ-name">${escapeHtml(name)}</span>` +
                `<span class="rc-champ-today">${today}</span>` +
                `<span class="rc-champ-pts">${fmtPts(row.predictedPoints)}</span>` +
                changeHtml(row.positionChange) +
                `</div>`;
        }

        // ── Drivers column ────────────────────────────────────────
        let leftHtml = `<div class="rc-champ-col-title">Drivers</div>`;
        drivers.forEach((row, i) => {
            if (row.num == null) return;
            const info = driverList[row.num] || {};
            const name = info.lastName || info.tla || `#${row.num}`;
            leftHtml += rowHtml(i + 1, info.teamColour, name, row);
        });

        // ── Constructors column ───────────────────────────────────
        let rightHtml = `<div class="rc-champ-col-title">Constructors</div>`;
        constructors.forEach((row, i) => {
            if (row.team == null) return;
            rightHtml += rowHtml(i + 1, teamColours[row.team], row.team, row);
        });

        return `<div class="rc-champ-cols">` +
                 `<div class="rc-champ-col">${leftHtml}</div>` +
                 `<div class="rc-champ-col">${rightHtml}</div>` +
               `</div>`;
    }

    function handleChampionship(data) {
        if (!data || typeof data !== 'object') return;
        champPayload = data;
        renderAll();
    }

    // ── Rendering ──────────────────────────────────────────────────

    function handleMessages(data) {
        if (!Array.isArray(data)) return;
        rcMessages = data;
        renderAll();
    }

    function renderAll() {
        const rcm = document.getElementById('rcPaneRcm');
        const peck = document.getElementById('rcPanePecking');
        const champ = document.getElementById('rcPaneChamp');
        if (!rcm) return;

        let rcHtml = '';
        for (const msg of rcMessages) {
            let colorClass = '';
            if (msg.color === 'yellow') colorClass = 'rc-yellow';
            else if (msg.color === 'red') colorClass = 'rc-red';
            else if (msg.color === 'green') colorClass = 'rc-green';
            else if (msg.color === 'blue') colorClass = 'rc-blue';
            else if (msg.color === 'chequered') colorClass = 'rc-chequered';
            else if (msg.color === 'orange') colorClass = 'rc-orange';
            // FIA stewards highlighting:
            //   - "UNDER INVESTIGATION" / "WILL BE INVESTIGATED" → yellow.
            //   - "INCIDENT ... NOTED" → no highlight (= not a penalty,
            //     just a record). The server processor's PENALTY-keyword
            //     match is too eager and flags NOTED messages because
            //     they often mention the word PENALTY in their cause
            //     (e.g. "NOTED - FAILING TO SERVE TIME PENALTY").
            //   - "PENALTY ... SERVED" → no highlight. Already-served
            //     penalties are after-the-fact records, not an active
            //     penalty being awarded; same upstream eager-match.
            const _msg = msg.message || '';
            if (/UNDER INVESTIGATION|WILL BE INVESTIGATED/i.test(_msg)) {
                colorClass = 'rc-yellow';
            } else if (/INCIDENT.*NOTED/i.test(_msg)) {
                colorClass = '';
            } else if (/PENALTY/i.test(_msg) && /SERVED/i.test(_msg)) {
                colorClass = '';
            }

            const timeStr = toLocalTimeStr(msg.timestamp);
            rcHtml += `<div class="race-control-msg ${colorClass}">` +
                `<span class="race-control-time">${timeStr}</span>` +
                `<span class="race-control-text">${msg.message}</span>` +
                `</div>`;
        }

        rcm.innerHTML = rcHtml || '<div class="rc-empty">No messages yet.</div>';
        rcm.scrollTop = rcm.scrollHeight;
        if (peck) {
            peck.innerHTML = peckingHtml
                || '<div class="rc-empty">Pecking order will appear after FP1 ends.</div>';
        }
        if (champ) {
            champ.innerHTML = buildChampHtml(champPayload)
                || '<div class="rc-empty">Championship standings appear once the race starts.</div>';
        }
    }

    function applyChampionshipTabVisibility() {
        // Championship tab only relevant in race-style sessions.
        const tab = document.querySelector('#rcTabs .rc-tab[data-tab="champ"]');
        const pane = document.getElementById('rcPaneChamp');
        const showChamp = isRaceSession();
        if (tab) tab.style.display = showChamp ? '' : 'none';
        if (pane && !showChamp) pane.classList.remove('active');
    }

    function applyPeckingTabVisibility() {
        const tab = document.querySelector('#rcTabs .rc-tab[data-tab="pecking"]');
        const pane = document.getElementById('rcPanePecking');
        const showPecking = isPeckingSession();
        if (tab) tab.style.display = showPecking ? '' : 'none';
        if (pane && !showPecking) pane.classList.remove('active');
    }

    function activateTab(tab) {
        document.querySelectorAll('#rcTabs .rc-tab').forEach((b) => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
        document.querySelectorAll('.rc-pane').forEach((p) => {
            p.classList.toggle('active', p.dataset.pane === tab);
        });
    }

    document.addEventListener('click', (e) => {
        const btn = e.target.closest('#rcTabs .rc-tab');
        if (!btn) return;
        activateTab(btn.dataset.tab);
        // Lazy fetch — guarantees the pane shows data even if the
        // initial DOMContentLoaded fetch fired before the pane existed.
        if (btn.dataset.tab === 'pecking' && !peckingHtml && isPeckingSession()) {
            fetchPriorPeckingOrder();
        }
    });

    // ── Wiring ─────────────────────────────────────────────────────

    messageBus.on('raceControlMessages', handleMessages);
    messageBus.on('championshipPrediction', handleChampionship);
    // Capture driverList so the Championship tab can resolve num→TLA.
    messageBus.on('driverList', (data) => {
        if (data && typeof data === 'object') {
            driverList = data;
            renderAll();
        }
    });
    // sessionInfo carries gmtOffset; once it arrives, re-render so
    // race-control timestamps switch from UTC to track-local.
    messageBus.on('sessionInfo', () => renderAll());

    messageBus.on('state:reset', () => {
        peckingHtml = '';
        rcMessages = [];
        champPayload = null;
        driverList = {};
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
