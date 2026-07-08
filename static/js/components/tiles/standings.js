/**
 * Unified Standings Tile
 *
 * One component for practice, qualifying, and race. Renders columns per
 * the spec from the user; session_type drives which columns are shown.
 *
 * Row order comes from the `standings` topic (ordering only); all row data
 * is joined client-side from per-driver topics into state.driverData /
 * state.timing (the "adapter" — server computes, client renders).
 *
 * Subscriptions:
 *   driverList                  — TLA + team colour
 *   standings                   — row order [{num, position}]
 *   qualifyingSegment           — current segment + eliminated[] (quali)
 *   raceLaps                    — current race lap (P1 cell)
 *   driverGap:NN / driverInt:NN — gap (+ knockout cutoff) / interval
 *   driverLaps:NN               — currentLap, laps map, lastLap, bestLap
 *   driverSectors:NN            — live S1/S2/S3 values + fastest flags
 *   driverMiniSectors:NN        — mini-sector segment colours (+ layout)
 *   currentTyre:NN / tyreHistory:NN — tyre stints
 *   driverStatus:NN             — DSQ/ELIMINATED/RET/STOP/OUT/PIT/FINISHED/TRACK
 *   driverLapClassification:NN  — PUSH / SLOW / OUT / PIT / STOP / "" (race)
 *   driverPenalties:NN          — penalty/flag indicators (race + sprint)
 *   lapPrediction:NN            — improving-PUSH delta + places gained (quali)
 *   fastestLap                  — session overall-fastest holder (purple)
 */

(function () {
    const SESSION_TYPE = (window.SESSION_CONFIG && SESSION_CONFIG.sessionType) || 'practice';
    const IS_RACE = SESSION_TYPE === 'race';
    const IS_QUALI = SESSION_TYPE === 'qualifying';
    const IS_PRACTICE = !IS_RACE && !IS_QUALI;

    // Server segment hex → the shared colour scale (.seg-bar.c-* paints it as bg).
    // The mini "white" is the suppressed state (slow/out/pre-race) → dimmed grey.
    const SEGMENT_COLOR_CLASS = {
        '#ffd700': 'c-yellow',
        '#00ff00': 'c-green',
        '#ff00ff': 'c-purple',
        '#ffffff': 'c-dim',
    };

    const state = {
        drivers: {},          // num → {tla, color, team}
        standingsOrder: [],   // [num, num, ...] from the `standings` topic
        driverData: {},       // num → assembled {gap, gapIsRed, interval, bestLap, …}
        timing: {},           // num → assembled current lap {lap, lapTime, bestLapTime, sectors[]}
        prevLap: {},          // num → snapshot of last completed lap (lapTime + sectors)
        sectorsCleared: {},   // num → bool: one-shot latch for the position-gain drop
        tyres: {},            // num → assembled tyre stints array (render)
        currentTyre: {},      // num → {compound, isNew, age}
        tyreHistory: {},      // num → [{compound, totalLaps, isNew}] past stints
        lapTimes: {},         // num → {lap → time_str} from driverLaps.laps
        status: {},           // num → DSQ/ELIMINATED/RET/STOP/OUT/PIT/FINISHED/TRACK
        lapCls: {},           // num → {lap, status} (latest classification type)
        prediction: {},       // num → lapPrediction {lap, delta, placesGained}
        currentLap: 0,        // race-only (from raceLaps)
        qualifyingSegment: null,
        eliminated: new Set(),
    };

    // ─── Helpers ───

    // Driver "finished" (chequered marker) is now authoritative from the
    // server (display:standings `finished`), computed in standings_processor
    // and seek-safe via state:restore. The old client-side derivation
    // (state.finishedDrivers / _prevChequeredCount / markDriverFinishedIfPassive)
    // is superseded and left dead pending cleanup — see issue #26.
    function isFinished(num) {
        // Server-authoritative: driver_status emits CHECKERED once a driver
        // starts a new lap under the chequered flag (race S/F crossing / P-Q first-car).
        return state.status[num] === 'CHECKERED';
    }

    function getTyreSvg(compound, isNew) {
        const c = (compound || 'unknown').toLowerCase();
        return `/static/images/tyres/${c}-${isNew ? 'new' : 'used'}.svg`;
    }

    function parseLapMs(s) {
        if (!s) return null;
        const m = s.match(/^(\d+):(\d+)\.(\d+)$/);
        if (!m) return null;
        return parseInt(m[1]) * 60000 + parseInt(m[2]) * 1000
             + parseInt(m[3].padEnd(3, '0').slice(0, 3));
    }

    function formatGap(ms) {
        const s = ms / 1000;
        return (s >= 0 ? '+' : '') + s.toFixed(3);
    }

    function ensureDriver(num) {
        if (!state.drivers[num]) {
            state.drivers[num] = {
                tla: num,
                color: TEAM_COLORS[num] || DEFAULT_CAR_COLOR,
                team: '',
            };
        }
        return state.drivers[num];
    }

    function tyreLaps(stint, currentLap) {
        // Stint records both startLaps (laps already on the tyre at session
        // start) and totalLaps (cumulative laps now). Use TotalLaps directly
        // when available; otherwise derive from currentLap - stint.lap.
        if (stint.totalLaps != null && stint.startLaps != null) {
            return Math.max(0, stint.totalLaps - stint.startLaps);
        }
        if (stint.totalLaps != null) return stint.totalLaps;
        if (stint.lap != null && currentLap > 0) {
            return Math.max(0, currentLap - stint.lap);
        }
        return 0;
    }

    // ─── Subscriptions ───

    // ── FIA Stewards stack (race + sprint) ────────────────────────────
    // Single global stack: each entry references the driver(s) it
    // applies to. Re-rendered on every update; blue-flag entries are
    // expiry-checked at render time against the current session clock.
    // Per-driver penalty/flag indicators (race + sprint). driver_status owns
    // DSQ now (its own state), so it's no longer a penalty kind here.
    let driverPenalties = {};   // num → [ {kind, label, color, reason, tooltip, tsMs, untilMs} ]

    messageBus.on('driverPenalties:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        driverPenalties[num] = Array.isArray(data) ? data : [];
        render();
    });

    // Pole-less waving-flag SVG — 16 × 16, same shape as scrubber flags.
    const _WAVING_FLAG_PATH = 'M1 3 Q4 1 8 3 T15 3 V13 Q12 15 8 13 T1 13 Z';
    function flagSvg(fill, extra) {
        return `<svg width="16" height="16" viewBox="0 0 16 16" `
             + `stroke="rgba(0,0,0,0.7)" stroke-width="0.5" `
             + `fill="${fill}">`
             + (extra || '')
             + `<path d="${_WAVING_FLAG_PATH}"/>`
             + `</svg>`;
    }
    const TRACK_LIMITS_FLAG_SVG =
        // Diagonal half-black / half-white flag. Render order: white
        // base (= dark outer stroke against light), then black
        // triangular overlay (= WHITE 0.5 px stroke so the black part
        // is visible against the dark UI background).
        `<svg width="16" height="16" viewBox="0 0 16 16" `
        + `stroke="rgba(0,0,0,0.7)" stroke-width="0.5">`
        + `<path d="${_WAVING_FLAG_PATH}" fill="#ffffff"/>`
        + `<path d="M1 3 L15 13 Q12 15 8 13 T1 13 Z" fill="#1a1a1a" `
        + `stroke="#ffffff" stroke-width="0.5"/>`
        + `</svg>`;
    const BLUE_FLAG_SVG = flagSvg('#1d77ff');

    function _indicatorPriority(kind) {
        // Per SME 2026-06-06: penalties (red) first, then yellow, then
        // white, then track-limits flag, then blue flag.
        if (kind === 'blackFlag') return -1;
        if (kind === 'dt' || kind === 'sg') return 0;
        if (typeof kind === 'string' && /^\d+s$/.test(kind)) return 0;
        if (kind === 'investigation') return 1;
        if (kind === 'deferred' || kind === 'noted') return 2;
        if (kind === 'trackLimits') return 3;
        if (kind === 'blueFlag') return 4;
        return 5;
    }
    function getDriverIndicators(num) {
        const list = driverPenalties[num];
        if (!list || !list.length) return [];
        const nowMs = messageBus.getCurrentOffset
            ? messageBus.getCurrentOffset() * 1000 : 0;
        const matches = [];
        for (const e of list) {
            // Blue flags expire; the server stamps untilMs (session-clock).
            if (e.kind === 'blueFlag'
                    && e.untilMs != null && e.untilMs < nowMs) continue;
            matches.push(e);
        }
        matches.sort((a, b) => {
            const pa = _indicatorPriority(a.kind);
            const pb = _indicatorPriority(b.kind);
            if (pa !== pb) return pa - pb;
            return (a.tsMs || 0) - (b.tsMs || 0);
        });
        return matches;
    }
    function isDriverDSQ(num) {
        // DSQ is now a driver_status state, not a penalty entry.
        return state.status[num] === 'DSQ';
    }
    function renderPenaltyStack(num) {
        // (Retired/finished penalty-stack suppression removed — client renders
        // whatever indicators the server sends; any suppression is a server
        // decision, TBD. ybTVoVep)
        if (isDriverDSQ(num)) {
            return `<span class="pen-stack"><span class="pen-badge pen-dsq" data-tooltip="DISQUALIFIED">DSQ</span></span>`;
        }
        const inds = getDriverIndicators(num);
        if (!inds.length) return '';
        const parts = ['<span class="pen-stack">'];
        for (const i of inds) {
            const tip = (i.tooltip || '').replace(/"/g, '&quot;');
            if (i.kind === 'trackLimits') {
                parts.push(`<span class="pen-flag" data-tooltip="${tip}">${TRACK_LIMITS_FLAG_SVG}</span>`);
            } else if (i.kind === 'blueFlag') {
                parts.push(`<span class="pen-flag" data-tooltip="${tip}">${BLUE_FLAG_SVG}</span>`);
            } else {
                const colorCls = `pen-${i.color || 'white'}`;
                parts.push(`<span class="pen-badge ${colorCls}" data-tooltip="${tip}">${i.label || ''}</span>`);
            }
        }
        parts.push('</span>');
        return parts.join('');
    }

    messageBus.on('driverList', (data) => {
        if (!data || typeof data !== 'object') return;
        for (const [num, info] of Object.entries(data)) {
            const d = ensureDriver(num);
            if (info.tla) d.tla = info.tla;
            if (info.teamColour) d.color = `#${info.teamColour}`;
            if (info.teamName) d.team = info.teamName;
        }
        render();
    });

    // Per-driver display fields (gap, bestLap, …) are now assembled from
    // individual topics rather than one fat display:standings row.
    function ensureData(num) {
        if (!state.driverData[num]) state.driverData[num] = {};
        return state.driverData[num];
    }

    // `standings` carries ordering only: [{num, position}], pre-sorted.
    messageBus.on('standings', (data) => {
        if (!data || !Array.isArray(data.drivers)) return;
        state.standingsOrder = data.drivers.map(e => String(e.num));
        for (const e of data.drivers) ensureData(String(e.num));
        render();
    });

    // qualifyingSegment {segment, eliminated:[nums], isSprintQuali}.
    messageBus.on('qualifyingSegment', (data) => {
        if (!data) return;
        const newElim = new Set((data.eliminated || []).map(String));
        // A new part starting clears best laps (server-driven). Clear gap + last
        // lap too, so the prior part's values don't linger — but NOT for
        // eliminated drivers, who keep their best + a frozen gap from the part
        // they were knocked out in (handled elsewhere).
        if (data.segment && data.segment !== state.qualifyingSegment) {
            for (const num of Object.keys(state.timing)) {
                if (newElim.has(num)) continue;
                const t = state.timing[num];
                t.lapTime = null; t.personalFastest = false; t.overallFastest = false;
                delete state.prevLap[num];
                state.sectorsCleared[num] = false;
            }
            for (const num of Object.keys(state.driverData)) {
                if (newElim.has(num)) continue;
                const e = state.driverData[num];
                e.gap = ''; e.gapIsRed = false; e.gapTrend = '';
            }
        }
        if (data.segment) state.qualifyingSegment = data.segment;
        state.eliminated = newElim;
        render();
    });

    // raceLaps {currentLap, totalLaps} — race lap counter (P1 "L{n}" cell).
    messageBus.on('raceLaps', (data) => {
        if (data && data.currentLap != null) {
            state.currentLap = data.currentLap;
            render();
        }
    });

    // driverGap:{num} {gap, cutoff, trend}; cutoff=in knockout zone → red gap;
    // trend (race) green=shrinking / yellow=growing vs previous value.
    messageBus.on('driverGap:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        const e = ensureData(num);
        e.gap = data.gap || '';
        e.gapIsRed = !!data.cutoff;
        e.gapTrend = data.trend || '';
        e.gapBand = data.band || '';
        render();
    });

    // driverInt:{num} {interval, trend} — interval to car ahead (race).
    messageBus.on('driverInt:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        const e = ensureData(num);
        // Tolerate the legacy bare-string payload as well as {interval, trend}.
        if (typeof data === 'string') { e.interval = data || ''; e.intTrend = ''; }
        else { e.interval = (data && data.interval) || ''; e.intTrend = (data && data.trend) || ''; }
        render();
    });

    // driverPaceColour:{num} {lap, colour} — race only. Last-lap pace band vs the
    // leader's reference lap (purple/blue/green/yellow/orange/red, white=in/out).
    messageBus.on('driverPaceColour:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        ensureData(num).paceColour = data.colour || null;
        render();
    });

    // driverBestLapColour:{num} {lap, colour} — server-computed best-lap colour
    // (atcmh1cL): purple=current fastest-overall holder, else Δ-to-fastest band;
    // null clears it. (Replaces the removed client fastestLap purple/PB tracking.)
    messageBus.on('driverBestLapColour:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        ensureData(num).bestLapColour = data.colour || null;
        render();
    });

    // driverSectorColour:{num} [c0,c1,c2] — server-computed per-sector colour
    // (atcmh1cL): vs best-overall in P/Q, vs the leader's same-lap in race; in/out
    // white. Client just applies sector-{colour}.
    messageBus.on('driverSectorColour:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        ensureData(num).sectorColour = Array.isArray(data) ? data : [];
        render();
    });

    // driverBestSectors:{num} [v0,v1,v2] + driverBestSectorColour:{num} [c0,c1,c2] —
    // each driver's fastest S1/S2/S3 + band colour vs the session-best sector
    // (best_sector_processor). Shown when a sector column header is toggled to "best".
    messageBus.on('driverBestSectors:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        ensureData(num).bestSectors = Array.isArray(data) ? data : [];
        render();
    });
    messageBus.on('driverBestSectorColour:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        ensureData(num).bestSectorColour = Array.isArray(data) ? data : [];
        render();
    });

    // driverLaps:{num} {currentLap, laps:{n:{time,personalBest,overallBest}},
    //                    lastLap:{lap,time,personalBest,overallBest}|null,
    //                    bestLap:{lap,time}|null}
    // Replaces driverTiming + driverLapTimes + driverLastLap. The current
    // lap's SECTORS are merged in by the driverSectors/driverMiniSectors
    // handlers (kept on state.timing[num].sectors); here we own the lap
    // number, lap-time, best lap, the per-lap times map, and the
    // completed-lap snapshot (taken at rollover from the live sectors).
    messageBus.on('driverLaps:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        const t = state.timing[num] || (state.timing[num] = {});
        const prevLapNum = t.lap;

        t.lap = data.currentLap;
        // bestLap is per-qualifying-part now and resets to null at each new part
        // (card 63) — clear on null so the prior part's best stops showing.
        t.bestLapTime = data.bestLap ? data.bestLap.time : null;
        if (data.lastLap) {
            t.lapTime = data.lastLap.time;
            t.personalFastest = data.lastLap.personalBest;
            t.overallFastest = data.lastLap.overallBest;
        } else {
            // Server blanked the last lap (e.g. new quali part) — clear it so the
            // cell blanks instead of holding the previous value. (hqb93XEw)
            t.lapTime = null;
            t.personalFastest = false;
            t.overallFastest = false;
        }

        // Per-lap times map {lapNum → time_str} (lapCount + prediction ref).
        // driverLaps is thin — accumulate from lastLap as laps arrive (a
        // seek/restore replays the full driverLaps history, and state:reset
        // wipes the map, so accumulation stays correct across seeks).
        if (data.lastLap && data.lastLap.lap != null && data.lastLap.time) {
            (state.lapTimes[num] || (state.lapTimes[num] = {}))[data.lastLap.lap] =
                data.lastLap.time;
        }

        // Best-lap display (purple/green decided at render via fastestLap).
        const e = ensureData(num);
        if (data.bestLap) {
            e.bestLap = data.bestLap.time;
            e.bestLapPersonal = true;
            e.bestLapNum = data.bestLap.lap;   // for out/in suppression at render
        } else {
            // Per-part reset (card 63): no best in the current part yet → clear.
            e.bestLap = null;
            e.bestLapNum = null;
            e.bestLapPersonal = false;
        }

        // Completed-lap snapshot — capture the live sectors (still holding
        // the just-finished lap's S1/S2/S3 at this instant) plus lastLap's
        // time/flags. Used by the race-mode segment-bar merge (segmentBarsCell).
        if (data.lastLap && data.lastLap.lap != null) {
            state.prevLap[num] = {
                lap: data.lastLap.lap,
                lapTime: data.lastLap.time,
                overallFastest: data.lastLap.overallBest,
                personalFastest: data.lastLap.personalBest,
                sectors: t.sectors ? t.sectors.map(s => ({ ...s })) : null,
            };
        }

        // Rollover → new lap starting: re-arm the sector-clear overlay so
        // the row keeps showing the previous lap until the new S1 lands.
        if (prevLapNum && data.currentLap && data.currentLap !== prevLapNum) {
            state.sectorsCleared[num] = false;
        }
        render();
    });

    // driverSectors:{num} [{value, overallFastest, personalFastest}] ×3 —
    // the live lap's sector times+flags. Merge into timing[num].sectors,
    // preserving the segment colours set by driverMiniSectors.
    messageBus.on('driverSectors:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !Array.isArray(data)) return;
        const t = state.timing[num] || (state.timing[num] = {});
        if (!t.sectors) t.sectors = [{}, {}, {}];
        if (data[0] && data[0].value && !state.sectorsCleared[num]) {
            state.sectorsCleared[num] = true;   // first new S1 → clear overlay
            // Drop a spent observed position-gain once the driver reaches S1 of
            // a lap AFTER the one that earned it — one-shot delete, so it can't
            // reappear later (e.g. after a pit stop / new run). (Card V2auWGhu.)
            const pg = state.prediction[num];
            if (pg && pg.observed && pg.lap != null && (t.lap || 0) > pg.lap) {
                delete state.prediction[num];
            }
        }
        for (let i = 0; i < 3; i++) {
            const seg = t.sectors[i] && t.sectors[i].segments;
            t.sectors[i] = { ...(data[i] || {}), segments: seg || [] };
        }
        render();
    });

    // driverMiniSectors:{num} [seg[], seg[], seg[]] — segment colour arrays.
    // Also derives the per-track mini-sector layout (replaces segmentLayout).
    messageBus.on('driverMiniSectors:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !Array.isArray(data)) return;
        const t = state.timing[num] || (state.timing[num] = {});
        if (!t.sectors) t.sectors = [{}, {}, {}];
        for (let i = 0; i < 3; i++) {
            t.sectors[i] = { ...(t.sectors[i] || {}), segments: data[i] || [] };
        }
        const layout = data.map(s => (Array.isArray(s) ? s.length : 0));
        if (layout.length === 3 && layout.some(n => n > 0)) {
            window.SEGMENT_LAYOUT = layout;
        }
        render();
    });

    // currentTyre:{num} {compound, isNew, age} + tyreHistory:{num} [stints]
    // → the tyre-stint array the render expects (current last). Each stint:
    // {compound, new, totalLaps}. tyreLaps() derives lap counts.
    function rebuildTyres(num) {
        const hist = state.tyreHistory[num] || [];
        const cur = state.currentTyre[num];
        const stints = hist.map(s => ({
            compound: s.compound, new: s.isNew, totalLaps: s.totalLaps,
        }));
        if (cur) {
            stints.push({
                compound: cur.compound, new: cur.isNew,
                totalLaps: cur.age, current: true,
            });
        }
        state.tyres[num] = stints;
    }
    messageBus.on('currentTyre:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        state.currentTyre[num] = data;
        rebuildTyres(num);
        render();
    });
    messageBus.on('tyreHistory:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !Array.isArray(data)) return;
        state.tyreHistory[num] = data;
        rebuildTyres(num);
        render();
    });

    // driverStatus:{num} — DSQ/ELIMINATED/RET/STOP/OUT/PIT/FINISHED/TRACK.
    // FINISHED (chequered) and DSQ are read directly by isFinished/isDriverDSQ.
    messageBus.on('driverStatus:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        state.status[num] = data;
        if (data === 'ELIMINATED') state.eliminated.add(num);
        // A spent observed position-gain is dropped when the driver STOPs or
        // pits (their run is over) so it doesn't linger or reappear on the way
        // back out. (Card V2auWGhu.)
        if (data === 'STOP' || data === 'PIT') {
            const pg = state.prediction[num];
            if (pg && pg.observed) delete state.prediction[num];
        }
        render();
    });

    // driverLapClassification:{num} {lap, trackPct, type}. type ∈
    // PUSH / SLOW / OUT / PIT / STOP / "" (race). Gate the current-lap
    // indicator so a retroactive finalize for an older lap can't regress it.
    messageBus.on('driverLapClassification:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        const curr = state.lapCls[num] || { lap: 0 };
        if (data.lap != null && data.lap >= (curr.lap || 0)) {
            state.lapCls[num] = { lap: data.lap, status: data.type };
        }
        render();
    });

    messageBus.on('lapPrediction:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        state.prediction[num] = data || {};
        render();
    });

    // (segment + eliminated come from the qualifyingSegment topic;
    // currentLap from raceLaps; finished/overall-fastest from the server —
    // so standings no longer needs sessionInfo.)

    messageBus.on('state:reset', () => {
        state.standingsOrder = [];
        state.driverData = {};
        state.timing = {};
        // Per-driver caches that accumulate across messages MUST be wiped
        // on state:reset so a backward seek doesn't leave stale data
        // (lap counts, sector times, last-lap snapshot) hanging around.
        state.lapTimes = {};
        state.prevLap = {};
        state.sectorsCleared = {};
        state.tyres = {};
        state.currentTyre = {};
        state.tyreHistory = {};
        state.status = {};
        state.lapCls = {};
        state.prediction = {};
        state.currentLap = 0;
        state.qualifyingSegment = null;
        state.eliminated = new Set();
        driverPenalties = {};
        render();
    });

    // ─── Render ───

    function emptySectorCells() {
        return '<span class="sector-time c-empty"></span>'.repeat(3);
    }

    function emptySegmentsHtml() {
        const layout = (window.SEGMENT_LAYOUT || [9, 5, 10]);
        let html = '';
        for (let si = 0; si < 3; si++) {
            if (si > 0) html += '<span class="seg-spacer"></span>';
            for (let j = 0; j < (layout[si] || 0); j++) {
                html += '<span class="seg-bar"></span>';
            }
        }
        return html;
    }

    // 16 × 14 chequered-flag SVG. Aspect ratio matches the scrubber
    // flag emoji (= shorter than tall feels more like a real flag).
    // 4 cols × 4 rows (= 4px × 3.5px cells, rounded to 4×3 visually).
    // 16 × 12, 4 cols × 4 rows (cells 4 × 3), with a 0.5px border around it.
    const CHEQUERED_SVG = '<svg class="st-chequered-svg" width="16" height="12" viewBox="0 0 16 12">'
        + '<rect width="16" height="12" fill="white"/>'
        + '<rect x="0"  y="0" width="4" height="3" fill="black"/>'
        + '<rect x="8"  y="0" width="4" height="3" fill="black"/>'
        + '<rect x="4"  y="3" width="4" height="3" fill="black"/>'
        + '<rect x="12" y="3" width="4" height="3" fill="black"/>'
        + '<rect x="0"  y="6" width="4" height="3" fill="black"/>'
        + '<rect x="8"  y="6" width="4" height="3" fill="black"/>'
        + '<rect x="4"  y="9" width="4" height="3" fill="black"/>'
        + '<rect x="12" y="9" width="4" height="3" fill="black"/>'
        + '<rect x="0.25" y="0.25" width="15.5" height="11.5" fill="none" stroke="rgba(0,0,0,0.7)" stroke-width="0.5"/>'
        + '</svg>';

    function statusCell(num) {
        // For race: pit in/out only.
        // For practice/quali: lap classification (PUSH/COOL/OUT/IN/PIT/ABORT).
        let base;
        // Chequered overrides everything (PIT/STOP/RET still get the
        // chequered indicator because they took the flag THEN pitted).
        if (isFinished(num)) {
            base = { text: CHEQUERED_SVG, cls: 'st-chequered' };
            const stackHtml = renderPenaltyStack(num);
            if (stackHtml) base.text = `${base.text} ${stackHtml}`;
            return base;
        }
        const st = state.status[num];
        if (st === 'PIT')      base = { text: 'PIT',  cls: 'st-pit'  };
        else if (st === 'STOP') base = { text: 'STOP', cls: 'st-stop' };
        else if (st === 'RET')  base = { text: 'RET',  cls: 'st-ret'  };
        else if (IS_RACE) {
            base = (st === 'OUT')
                ? { text: 'OUT', cls: 'st-out' }
                : { text: '',    cls: 'st-track' };
        } else {
            const lc = state.lapCls[num];
            if (lc && lc.status) {
                // type ∈ PUSH / SLOW / OUT / PIT / STOP
                const s = lc.status;
                base = { text: s, cls: `st-${s.toLowerCase()}` };
            } else if (st === 'OUT') base = { text: 'OUT',  cls: 'st-out'  };
            else if (st === 'TRACK') base = { text: 'PUSH', cls: 'st-push' };
            else                     base = { text: '',     cls: ''        };
        }
        // Penalty/flag stack (race + sprint): pull from the fiaStewards
        // topic. Renders as a horizontal series of badges + flag icons.
        // Falls back to the legacy single-indicator (penaltyText) when
        // the stack is empty (= e.g. on session types that don't run
        // FiaStewardsProcessor).
        const d = state.driverData[num] || {};
        const stackHtml = renderPenaltyStack(num);
        if (stackHtml) {
            base.text = base.text ? `${base.text} ${stackHtml}` : stackHtml;
        } else if (d.penaltyText) {
            const pen = `<span class="status-flag flag-${d.penaltyClass}">${d.penaltyText}</span>`;
            base.text = base.text ? `${base.text} ${pen}` : pen;
        }
        return base;
    }

    // P1-delta colour bands. deltaMs = value − reference (>=0). `sector` uses the
    // tighter sector thresholds (card cKNdwUoZ); otherwise the gap/best/last
    // thresholds (card IeBKH1Xz). Returns a band-* class or null.
    // (bandClass / parseSectorMs / fastestSectors removed — the client-derived
    // Δ-to-fastest colour bands moved server-side. atcmh1cL)

    const BEST_LAP_COLOUR_CLASS = {
        purple: 'c-purple', blue: 'c-blue', green: 'c-green',
        yellow: 'c-yellow', orange: 'c-orange', red: 'c-red',
        white: 'c-white',   // eliminated driver's best — full-opacity neutral
    };

    function bestLapCell(num) {
        const e = state.driverData[num] || {};
        const t = state.timing[num] || {};
        const txt = e.bestLap || t.bestLapTime || '';
        // Server-emitted best-lap colour (atcmh1cL): purple = fastest-overall
        // holder, else Δ-to-fastest band. Client just applies the class.
        const cls = txt ? (BEST_LAP_COLOUR_CLASS[e.bestLapColour] || '') : 'c-empty';
        return `<span class="lap-time ${cls}">${txt || '--:--.---'}</span>`;
    }

    function gapCellEmpty() {
        return '<span class="gap c-empty">--.---</span>';
    }

    // (isSlowLapClass / lapTypeAt / isRetired removed — client-side suppression
    // helpers. chooseLapForDisplay also gone earlier. Retired/finished/slow-lap
    // blanking is a server decision, TBD. ybTVoVep)

    function lastLapCell(num) {
        // (Retired + eliminated blanking removed — client renders the server's
        // last-lap as-is; suppression moves server-side. ybTVoVep)
        // Spec depends on session type:
        //   - PRACTICE / QUALIFYING: show the actual lap time including
        //     in-pit and out laps (card 81); cool-down (SLOW) falls back to
        //     the previous fast lap so a cool lap doesn't read as pace.
        //   - RACE: ALWAYS show the latest lap data including IN/OUT,
        //     because every lap matters for race position + gap, and
        //     IN/OUT laps still produce times the engineer cares about.
        // Render the CURRENT last-lap directly — no prev-fast which-value
        // selection, no STOP suppression (those move server-side). Race keeps the
        // server-provided pace colour; quali/practice colour will come from the
        // server too (atcmh1cL). (ybTVoVep / atcmh1cL)
        const cur = state.timing[num];
        const last = (cur && cur.lapTime) || '';
        // Server-emitted pace class (race_pace for race, pq_pace for P/Q): vs the
        // leader's lap in race, vs the fastest overall in P/Q; in/out white.
        const pc = (state.driverData[num] || {}).paceColour;
        // "blank" = server suppression (retired/finished/eliminated, P/Q out/in/slow).
        if (pc === 'blank') {
            return `<span class="lap-time lap-last c-empty">--:--.---</span>`;
        }
        let cls = 'c-empty';
        if (last) {
            // pace "white" (slow / out / no colour yet) → dimmed grey; else the band.
            cls = (!pc || pc === 'white') ? 'c-dim' : `c-${pc}`;
        }
        return `<span class="lap-time lap-last ${cls}">${last || '--:--.---'}</span>`;
    }

    // (lastVsBestCell removed — unused dead code; its last-vs-best delta colour
    // was client-derived. atcmh1cL)

    function sectorCells(num) {
        // Render the CURRENT lap's sectors + apply the server-emitted per-sector
        // colour (driverSectorColour): vs best-overall in P/Q, vs the leader's
        // same-lap in race; in/out white. (atcmh1cL)
        const lap = state.timing[num];
        const sectors = (lap && lap.sectors) || [{}, {}, {}];
        const colours = (state.driverData[num] || {}).sectorColour || [];
        const bestVals = (state.driverData[num] || {}).bestSectors || [];
        const bestCols = (state.driverData[num] || {}).bestSectorColour || [];
        const mode = state.sectorMode || [false, false, false];
        const out = [];
        for (let i = 0; i < 3; i++) {
            let v, c, white = false;
            if (mode[i]) {
                v = bestVals[i] || '';               // best-sector mode: driver's fastest S{i}
                c = bestCols[i];
            } else {
                const s = sectors[i] || {};
                v = s.value || '';
                c = colours[i];
                white = !!s.white;                   // slow / out / post-flag → dimmed
            }
            let cls;
            if (!v) cls = 'c-empty';
            else if (white) cls = 'c-dim';
            // P/Q out/in/stop sector colour is also "white" → dimmed grey.
            else cls = c === 'white' ? 'c-dim' : (c ? `c-${c}` : '');
            out.push(`<span class="sector-time ${cls}">${v || '--.---'}</span>`);
        }
        return out.join('');
    }

    function segmentBarsCell(num) {
        // (Retired/finished blanking + slow-lap uncolour removed — client renders
        // the server's segment data as-is; suppression moves server-side. ybTVoVep)
        const t = state.timing[num] || {};
        let sectors = t.sectors || [];
        // Race-mode merge: same as sectorCells — fall back to the
        // previous lap's segment list per-slot so S2/S3 mini-sector
        // bars don't blank out at every new-lap reset.
        if (IS_RACE) {
            const prevAny = state.prevLap[num];
            if (prevAny && prevAny.sectors) {
                sectors = [0, 1, 2].map((i) => {
                    const cur = sectors[i] || {};
                    if (cur && cur.segments && cur.segments.length) return cur;
                    return prevAny.sectors[i] || cur;
                });
            }
        }
        const layout = (window.SEGMENT_LAYOUT || [9, 5, 10]);
        let html = '';
        for (let si = 0; si < 3; si++) {
            if (si > 0) html += '<span class="seg-spacer"></span>';
            const segs = (sectors[si] && sectors[si].segments) || [];
            const cnt = layout[si] || 0;
            for (let j = 0; j < cnt; j++) {
                const seg = j < segs.length ? segs[j] : null;
                const cls = seg ? (SEGMENT_COLOR_CLASS[seg] || '') : '';
                html += `<span class="seg-bar ${cls}"></span>`;
            }
        }
        return html;
    }

    function tyreCell(num, currentTyreOnly) {
        const stints = state.tyres[num];
        if (!stints || !stints.length) return '';
        const t = state.timing[num] || {};
        const curLap = t.lap || 0;
        // Current = last entry (or first marked .current); display first.
        const ordered = stints.slice().reverse();
        const slice = currentTyreOnly ? ordered.slice(0, 1) : ordered;
        return slice.map(stint => {
            if (!stint || !stint.compound) return '';
            const laps = tyreLaps(stint, curLap);
            return `<span class="tyre-stint">` +
                `<img class="tyre-stint-icon" src="${getTyreSvg(stint.compound, stint.new)}" alt="${stint.compound}">` +
                `<span class="tyre-stint-laps">${laps}</span>` +
                `</span>`;
        }).join('');
    }

    function predictionCell(num) {
        // The .pred span is ONE grid cell — delta + projected-position
        // side by side, otherwise inner spans would spill across grid
        // tracks and misalign every column to the right.
        //
        // Per SME 2026-06-07: only show delta on PUSH laps (= active
        // attempts), AND only when the driver has at least one timed
        // lap on the board THIS session (= a reference for the delta
        // to be meaningful).
        // lapPrediction {lap, delta (ms, negative), placesGained}. The
        // server only emits it for an improving PUSH lap, with the position
        // gain already computed (no client-side rank math needed).
        const p = state.prediction[num];
        if (!p || p.delta === undefined || p.delta === null) {
            return '<span class="pred"></span>';
        }
        // (PUSH/PIT blanking removed — lap_prediction clears the prediction
        // server-side on pit/retire/leaving-push, so the client just renders what
        // it's sent. ybTVoVep) The observed result (observed=true) shows regardless
        // of current class (cards 62/67).
        if (!p.observed) {
            const driverEntry = state.driverData[num] || {};
            const hasReference = Boolean(driverEntry.bestLap)
                || (state.lapTimes[num] && Object.keys(state.lapTimes[num]).length > 0);
            if (!hasReference) {
                return '<span class="pred"></span>';
            }
        }
        // Positions gained: green up-triangle + WHITE count, right-aligned.
        // Predicted (live projection) gain = green; observed (completed) = white.
        const gainCls = p.observed ? 'pred-pos-observed' : 'pred-pos-predicted';
        const posHtml = (p.placesGained != null && p.placesGained > 0)
            ? `<span class="pred-pos-gain ${gainCls}"><span class="pred-pos-arrow">&#9650;</span>`
              + `<span class="pred-pos-num">${p.placesGained}</span></span>`
            : '';
        // On completion (observed) only the positions gained are shown — the delta
        // time is dropped, but its slot is kept (empty) so the positions stay in
        // the same fixed column position as during the live projection.
        if (p.observed) {
            // The observed positions-gained is removed by DELETING
            // state.prediction[num] once the driver reaches S1 of the next lap,
            // or STOPs/pits (see the driverSectors / driverStatus handlers) — a
            // one-shot removal so it can't reappear after a pit/new run. Until
            // then, show it (empty delta slot keeps the column aligned).
            return `<span class="pred"><span class="pred-delta"></span>${posHtml}</span>`;
        }
        // Live projection: delta (0.1s) on the left, positions gained on the right.
        const deltaSec = p.delta / 1000;
        const sign = deltaSec < 0 ? '−' : '+';
        const deltaText = `${sign}${Math.abs(deltaSec).toFixed(1)}`;
        const deltaCls = deltaSec < 0 ? 'pred-delta-neg' : 'pred-delta-pos';
        return `<span class="pred">`
            + `<span class="pred-delta ${deltaCls}">${deltaText}</span>`
            + posHtml
            + `</span>`;
    }

    function formatLapTimeOneDecimal(ms) {
        if (!ms) return '';
        const totalSec = ms / 1000;
        const min = Math.floor(totalSec / 60);
        const sec = totalSec - min * 60;
        const secStr = sec < 10 ? `0${sec.toFixed(1)}` : sec.toFixed(1);
        return `${min}:${secStr}`;
    }

    function gapCell(num) {
        const e = state.driverData[num] || {};
        const txt = e.gap || '';
        if (!txt) return '<span class="gap c-empty">+-.---</span>';
        // Eliminated (quali): gap is the frozen bubble gap, shown full white.
        if (state.eliminated && state.eliminated.has(num)) {
            return `<span class="gap c-white">${txt}</span>`;
        }
        // Elimination zone → red (red is reserved for the zone only).
        if (e.gapIsRed) return `<span class="gap c-red">${txt}</span>`;
        // P/Q + practice: server-emitted Δ-to-P1 band (blue/green/yellow/orange;
        // red is reserved for the elimination zone above). atcmh1cL.
        const band = e.gapBand ? ` c-${e.gapBand}` : '';
        return `<span class="gap${band}">${txt}</span>`;
    }

    function penaltiesCell(num) {
        const e = state.driverData[num] || {};
        const items = [];
        if (e.underInvestigation) items.push('<span class="pen pen-investigation">⚠</span>');
        if (e.penalty) items.push(`<span class="pen pen-${(e.penalty.type || '').toLowerCase()}">${e.penalty.label || e.penalty}</span>`);
        if (e.trackLimitsWarning) items.push('<span class="pen pen-tl">TL</span>');
        if (e.blackFlag) items.push('<span class="pen pen-black">BLK</span>');
        return `<span class="pens">${items.join('')}</span>`;
    }

    function lapCountCell(num) {
        const t = state.timing[num] || {};
        // Lap count = the authoritative NoL-based current lap (driverLaps.currentLap,
        // stored as t.lap). (laps-down colour was client-derived → removed; the
        // server will emit the lap-counter colour. atcmh1cL)
        const n = t.lap || 0;
        return `<span class="lap-count">${n || '0'}</span>`;
    }

    function gapOrLapForRaceP1(num, position) {
        if (position === 1) {
            return `<span class="gap p1-lap">L${state.currentLap || ''}</span>`;
        }
        const e = state.driverData[num] || {};
        // 7-colour lap-over-lap trend (server-computed): purple/blue/green (catching)
        // white (flat) yellow/orange/red (dropping back). (t46cHyov)
        const t = e.gapTrend ? ` c-${e.gapTrend}` : '';
        return `<span class="gap${t}">${e.gap || ''}</span>`;
    }

    function intervalCell(num) {
        const e = state.driverData[num] || {};
        // 7-colour battle trend (server-computed): closing green→blue→purple,
        // opening (any band) yellow, white out of range. (t46cHyov)
        const t = e.intTrend ? ` c-${e.intTrend}` : '';
        return `<span class="interval${t}">${e.interval || ''}</span>`;
    }

    function buildRow(num, idx) {
        const drv = ensureDriver(num);
        const elim = state.eliminated.has(num);
        const cls = ['driver-row'];
        // Position 1 (= P1) gets its own class so the lap-time cells
        // can be styled purple in P/Q via CSS without needing to know
        // the "overall best" tracking (= which can drift on retroactive
        // reclass / restore-on-seek). idx is the 0-based render index.
        if (idx === 0) cls.push('p1');
        if (elim) cls.push('knocked-out');
        if (isDriverDSQ(num)) cls.push('dsq');

        let cols = '';
        // Start identifier block: rank · colour · TLA · car number.
        cols += `<span class="rank">${idx + 1}</span>`;
        cols += `<span class="driver-color" style="--team-color:${drv.color}"></span>`;
        cols += `<span class="driver-tla">${drv.tla}</span>`;
        cols += `<span class="driver-num">${num}</span>`;

        // Canonical column order (all session types):
        //   ... | mini-sectors | sectors | lap time | <session tail>
        if (IS_RACE) {
            // (Penalties column removed — penalty / under-investigation
            // indicator is now layered on the status badge itself.)
            const stt = statusCell(num);
            cols += `<span class="status ${stt.cls}">${stt.text}</span>`;
            cols += gapOrLapForRaceP1(num, idx + 1);
            cols += '<span class="col-spacer"></span>';
            cols += intervalCell(num);
            cols += '<span class="col-spacer"></span>';   // int ↔ last-lap gap
            cols += lastLapCell(num);
            cols += '<span class="col-spacer"></span>';   // last-lap ↔ sectors gap
            cols += sectorCells(num);
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += bestLapCell(num);
            // Spacer column — visually separates best-lap from the
            // tyre history (matches the spacing between lap-time and
            // best-lap).
            cols += '<span class="col-spacer"></span>';
            cols += `<span class="tyres">${tyreCell(num, false)}</span>`;
        } else if (IS_QUALI) {
            const stt = statusCell(num);
            cols += `<span class="status ${stt.cls}">${stt.text}</span>`;
            cols += bestLapCell(num);
            cols += gapCell(num);
            cols += lastLapCell(num);
            cols += predictionCell(num);
            cols += sectorCells(num);
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += `<span class="tyres">${tyreCell(num, true)}</span>`;
        } else {
            // Practice
            const stt = statusCell(num);
            cols += `<span class="status ${stt.cls}">${stt.text}</span>`;
            cols += bestLapCell(num);
            cols += '<span class="col-spacer"></span>';   // best-lap ↔ gap gap
            cols += gapCell(num);
            cols += lastLapCell(num);
            cols += '<span class="col-spacer"></span>';   // last-lap ↔ sectors gap
            cols += sectorCells(num);
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += `<span class="tyres">${tyreCell(num, false)}</span>`;
        }

        // Laps column + trailing spacer — all session types. Quali shows
        // the current tyre in a narrower tyre column but still shows the
        // lap count; each grid template includes the matching Laps +
        // laps-end-spacer tracks.
        cols += lapCountCell(num);
        cols += '<span class="col-spacer"></span>';

        // End identifier block, MIRRORED (reversed vs the start): TLA · colour ·
        // rank. So each driver is identifiable regardless of how wide / scrolled the
        // row is, and the rank bookends both sides. No car number at this end.
        cols += `<span class="driver-tla driver-tla-end">${drv.tla}</span>`;
        cols += `<span class="driver-color driver-color-end" style="--team-color:${drv.color}"></span>`;
        cols += `<span class="rank rank-end">${idx + 1}</span>`;

        return `<div class="${cls.join(' ')}" data-driver="${num}">${cols}</div>`;
    }

    // Header — column count must match the row's grid template for that
    // session type (see standings.css). Driver-identification cells get
    // no header text (rank/colour/tla/status are self-explanatory).
    // One sector's header cell: a current↔best toggle (S{n} / BS{n}). Defaults to
    // current; the active button is white, the other grey (shared .tile-btn style).
    function sectorHeaderCell(i) {
        const best = (state.sectorMode || [])[i];
        return '<span class="sec-toggle">' +
            `<button class="tile-btn sec-btn${best ? '' : ' active'}" data-sec="${i}" data-best="0">S${i + 1}</button>` +
            `<button class="tile-btn sec-btn${best ? ' active' : ''}" data-sec="${i}" data-best="1">BS${i + 1}</button>` +
            '</span>';
    }

    function buildHeader() {
        // Header order MUST match buildRow's column order.
        // Canonical: ... | Mini | S1 | S2 | S3 | Lap time | <tail>
        // Common left identifier-block header (= rank empty, "Driver"
        // spanning colour+tla, status empty). Explicit grid-column on
        // the Driver span keeps subsequent spans flowing into col 4+.
        const idHdr =
            '<span></span>' +                                        /* rank */
            '<span style="grid-column: 2 / span 3">Driver</span>' +  /* color + tla + num */
            '<span></span>';                                         /* status */

        if (IS_RACE) {
            return (
                '<div class="driver-header">' +
                idHdr +
                '<span>Gap</span>' +
                '<span></span>' + /* gap-int-spacer */
                '<span>Int</span>' +
                '<span></span>' + /* int-lap-spacer */
                '<span>Lap time</span>' +
                '<span></span>' + /* lap-sec-spacer */
                sectorHeaderCell(0) + sectorHeaderCell(1) + sectorHeaderCell(2) +
                '<span>Mini-sectors</span>' +
                '<span>Best lap</span>' +
                '<span class="col-spacer"></span>' +
                '<span>Tyres</span>' +
                '<span>Laps</span>' +
                '<span></span>' + /* laps-end-spacer */
                '<span></span><span></span><span></span>' + /* tla-end + color-end + rank-end */
                '</div>'
            );
        }
        if (IS_QUALI) {
            return (
                '<div class="driver-header">' +
                idHdr +
                '<span>Best lap</span>' +
                '<span>Gap</span>' +
                '<span>Lap time</span>' +
                '<span>Delta</span>' +
                sectorHeaderCell(0) + sectorHeaderCell(1) + sectorHeaderCell(2) +
                '<span>Mini-sectors</span>' +
                '<span>Tyre</span>' +
                '<span>Laps</span>' +
                '<span></span>' + /* laps-end-spacer */
                '<span></span><span></span><span></span>' + /* tla-end + color-end + rank-end */
                '</div>'
            );
        }
        // Practice.
        return (
            '<div class="driver-header">' +
            idHdr +
            '<span>Best lap</span>' +
            '<span></span>' + /* best-gap spacer */
            '<span>Gap</span>' +
            '<span>Lap time</span>' +
            '<span></span>' + /* lap-sec spacer */
            sectorHeaderCell(0) + sectorHeaderCell(1) + sectorHeaderCell(2) +
            '<span>Mini-sectors</span>' +
            '<span>Tyres</span>' +
            '<span>Laps</span>' +
            '<span></span>' + /* laps-end-spacer */
            '<span></span><span></span>' + /* color-end + tla-end */
            '</div>'
        );
    }

    // ─── Tooltip ───
    // The standings re-render wholesale (container.innerHTML) on every
    // update, which destroys the element being hovered and cancels a
    // native `title` tooltip mid-hover (#6). Use one persistent tooltip
    // element on <body> plus a cursor-position-driven tracker, re-asserted
    // after each render so a row rebuilt under a stationary cursor keeps
    // its tooltip. `pointer-events: none` on the tooltip keeps it out of
    // elementFromPoint.
    let _tooltipEl = null;
    let _mouseX = 0, _mouseY = 0, _mouseInside = false;

    function _ensureTooltip() {
        if (!_tooltipEl) {
            _tooltipEl = document.createElement('div');
            _tooltipEl.className = 'st-tooltip';
            _tooltipEl.style.display = 'none';
            document.body.appendChild(_tooltipEl);
        }
        return _tooltipEl;
    }

    function _updateTooltip() {
        const tip = _ensureTooltip();
        if (!_mouseInside) { tip.style.display = 'none'; return; }
        const el = document.elementFromPoint(_mouseX, _mouseY);
        const host = el && el.closest ? el.closest('[data-tooltip]') : null;
        const text = host && host.getAttribute('data-tooltip');
        if (!text) { tip.style.display = 'none'; return; }
        tip.textContent = text;
        tip.style.display = 'block';
        // Offset from the cursor, clamped to the viewport. clientX/Y pair
        // with position: fixed (viewport-relative).
        const pad = 12;
        const r = tip.getBoundingClientRect();
        let x = _mouseX + pad;
        let y = _mouseY + pad;
        if (x + r.width > window.innerWidth) x = _mouseX - r.width - pad;
        if (y + r.height > window.innerHeight) y = _mouseY - r.height - pad;
        tip.style.left = `${Math.max(0, x)}px`;
        tip.style.top = `${Math.max(0, y)}px`;
    }

    function _setupTooltip(container) {
        if (container._stTooltipBound) return;
        container._stTooltipBound = true;
        // Per-sector header toggle (S{n} ↔ BS{n}). Delegated so it survives the
        // wholesale re-render; each sector toggles current↔best independently.
        container.addEventListener('click', (e) => {
            const btn = e.target.closest('.tile-btn[data-sec]');
            if (!btn) return;
            const i = parseInt(btn.dataset.sec, 10);
            const best = btn.dataset.best === '1';
            const mode = state.sectorMode || (state.sectorMode = [false, false, false]);
            if (mode[i] !== best) { mode[i] = best; render(); }
        });
        container.addEventListener('mousemove', (e) => {
            _mouseX = e.clientX;
            _mouseY = e.clientY;
            _mouseInside = true;
            _updateTooltip();
        });
        container.addEventListener('mouseleave', () => {
            _mouseInside = false;
            _updateTooltip();
        });
    }

    // Throttle: the row data is assembled from ~12 per-driver topics, each
    // firing for 20 cars many times a second. Rendering on every one floods
    // the main thread (whole-table innerHTML rebuild) and freezes the UI, so
    // coalesce all updates within a frame into a single render.
    let _renderPending = false;
    function render() {
        if (_renderPending) return;
        _renderPending = true;
        requestAnimationFrame(() => { _renderPending = false; renderNow(); });
    }

    function renderNow() {
        const container = document.getElementById('driverList');
        if (!container) return;

        const order = state.standingsOrder.length
            ? state.standingsOrder
            : Object.keys(state.drivers);

        if (!order.length) {
            container.innerHTML = '<div class="loading-message">Waiting for data...</div>';
            return;
        }

        container.classList.add(`standings-${SESSION_TYPE}`);
        container.innerHTML = buildHeader() +
            order.map((num, i) => buildRow(num, i)).join('');

        // Bind hover tracking once; re-assert the tooltip in case a row
        // was rebuilt under a stationary cursor.
        _setupTooltip(container);
        _updateTooltip();
    }

    // (Mini-sector layout is derived from driverMiniSectors array lengths in
    // its handler — the standalone segmentLayout topic is gone.)
})();
