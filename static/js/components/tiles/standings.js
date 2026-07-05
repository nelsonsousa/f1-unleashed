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

    const SEGMENT_COLOR_CLASS = {
        '#ffd700': 'seg-yellow',
        '#00ff00': 'seg-green',
        '#ff00ff': 'seg-purple',
        '#ffffff': 'seg-white',
    };

    const state = {
        drivers: {},          // num → {tla, color, team}
        standingsOrder: [],   // [num, num, ...] from the `standings` topic
        driverData: {},       // num → assembled {gap, gapIsRed, interval, bestLap, …}
        timing: {},           // num → assembled current lap {lap, lapTime, bestLapTime, sectors[]}
        prevLap: {},          // num → snapshot of last completed lap (lapTime + sectors)
        prevFastLap: {},      // num → snapshot of last non-cool lap
        sectorsCleared: {},   // num → bool: have we cleared prev-lap sectors yet
        tyres: {},            // num → assembled tyre stints array (render)
        currentTyre: {},      // num → {compound, isNew, age}
        tyreHistory: {},      // num → [{compound, totalLaps, isNew}] past stints
        lapTimes: {},         // num → {lap → time_str} from driverLaps.laps
        status: {},           // num → DSQ/ELIMINATED/RET/STOP/OUT/PIT/FINISHED/TRACK
        lapCls: {},           // num → {lap, status} (latest classification type)
        lapClsByLap: {},      // num → {lapNum → type} per-lap map
        prediction: {},       // num → lapPrediction {lap, delta, placesGained}
        currentLap: 0,        // race-only (from raceLaps)
        qualifyingSegment: null,
        eliminated: new Set(),
        // Overall-fastest lap (from the server `fastestLap` topic) so we can
        // purple-tint the holder and a lap that matches it.
        overallBestLapMs: null,
        overallBestLapDriver: null,
    };

    // ─── Helpers ───

    // Driver "finished" (chequered marker) is now authoritative from the
    // server (display:standings `finished`), computed in standings_processor
    // and seek-safe via state:restore. The old client-side derivation
    // (state.finishedDrivers / _prevChequeredCount / markDriverFinishedIfPassive)
    // is superseded and left dead pending cleanup — see issue #26.
    function isFinished(num) {
        // Server-authoritative: driver_status emits FINISHED once a driver
        // has taken the chequered flag (race S/F crossing / P-Q first-car).
        return state.status[num] === 'FINISHED';
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
        // Retired drivers (RET / STOP / DSQ) — penalties / investigations
        // no longer matter. Clear the stack from their status badge.
        if (typeof isRetired === 'function' && isRetired(num)) return '';
        // Drivers who have taken the chequered flag: any remaining time
        // penalty is just added to their race time, not a pending action.
        // Investigations + flags also stop mattering once they're done.
        // (`finishedDrivers` is populated as each driver crosses S/F under
        // chequered, not session-wide at the flag — see markDriverFinishedIfPassive
        // and the driverLastLap handler.)
        if (isFinished(num)) return '';
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
                delete state.prevFastLap[num];
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

    // fastestLap {num, lap, time} — the session's overall-fastest holder.
    messageBus.on('fastestLap', (data) => {
        if (!data || data.num == null) return;
        state.overallBestLapDriver = String(data.num);
        state.overallBestLapMs = parseLapMs(data.time);
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
        // time/flags. prevFastLap only if that lap wasn't COOL/ABORT/OUT/IN.
        if (data.lastLap && data.lastLap.lap != null) {
            const snap = {
                lap: data.lastLap.lap,
                lapTime: data.lastLap.time,
                overallFastest: data.lastLap.overallBest,
                personalFastest: data.lastLap.personalBest,
                sectors: t.sectors ? t.sectors.map(s => ({ ...s })) : null,
            };
            state.prevLap[num] = snap;
            const cls = (state.lapClsByLap[num] || {})[data.lastLap.lap];
            if (!cls || (cls !== 'COOL' && cls !== 'ABORT'
                    && cls !== 'OUT' && cls !== 'IN')) {
                state.prevFastLap[num] = snap;
            }
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
    // indicator so a retroactive finalize for an older lap can't regress
    // it; keep a per-lap map for the prevFastLap decision.
    messageBus.on('driverLapClassification:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        const curr = state.lapCls[num] || { lap: 0 };
        if (data.lap != null && data.lap >= (curr.lap || 0)) {
            state.lapCls[num] = { lap: data.lap, status: data.type };
        }
        if (data.lap != null) {
            const map = state.lapClsByLap[num] || {};
            map[data.lap] = data.type;
            state.lapClsByLap[num] = map;
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
        state.prevFastLap = {};
        state.sectorsCleared = {};
        state.tyres = {};
        state.currentTyre = {};
        state.tyreHistory = {};
        state.status = {};
        state.lapCls = {};
        state.lapClsByLap = {};
        state.prediction = {};
        state.currentLap = 0;
        state.qualifyingSegment = null;
        state.eliminated = new Set();
        state.overallBestLapMs = null;
        state.overallBestLapDriver = null;
        driverPenalties = {};
        render();
    });

    // ─── Render ───

    // Practice / qualifying only: when the driver isn't on a flying lap
    // (out lap, slow/cool-down, in-pit, etc.) the current-lap timing
    // data is irrelevant. Hide sectors / last-lap / Δ / mini-segments so
    // the row only shows persistent info (best lap, gap, tyres).
    const SLOW_CLASSIFICATIONS = new Set(['OUT', 'SLOW']);
    function isSlowLap(num) {
        if (state.status[num] === 'PIT') return true;
        if (state.status[num] === 'STOP' || state.status[num] === 'RET') return true;
        const lc = state.lapCls[num];
        return !!(lc && SLOW_CLASSIFICATIONS.has(lc.status));
    }

    function emptySectorCells() {
        return '<span class="sector-time sector-empty"></span>'.repeat(3);
    }

    function emptySegmentsHtml() {
        const layout = (window.SEGMENT_LAYOUT || [9, 5, 10]);
        let html = '';
        for (let si = 0; si < 3; si++) {
            if (si > 0) html += '<span class="seg-spacer"></span>';
            for (let j = 0; j < (layout[si] || 0); j++) {
                html += '<span class="seg-bar seg-empty"></span>';
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
    // Δ-to-P1 colour bands (ms): <0.1 blue, 0.1-0.3 green, 0.3-0.5 yellow,
    // 0.5-1.0 orange, >1.0 red. `capOrange` clamps red→orange (best-lap/gap in
    // quali reserve red for the elimination zone).
    function bandClass(deltaMs, capOrange) {
        if (deltaMs == null || isNaN(deltaMs) || deltaMs < 0) return null;
        if (deltaMs < 100) return 'band-blue';
        if (deltaMs < 300) return 'band-green';
        if (deltaMs < 500) return 'band-yellow';
        if (deltaMs < 1000 || capOrange) return 'band-orange';
        return 'band-red';
    }

    function parseSectorMs(v) {
        if (!v) return null;
        const f = parseFloat(v);
        return isNaN(f) ? null : Math.round(f * 1000);
    }

    // Fastest current sector time (ms) per S1/S2/S3 across all drivers — the
    // reference for the sector colour bands. Cheap (drivers × 3) per render.
    function fastestSectors() {
        const best = [null, null, null];
        for (const n of Object.keys(state.timing)) {
            const secs = (state.timing[n] || {}).sectors;
            if (!Array.isArray(secs)) continue;
            for (let i = 0; i < 3; i++) {
                const ms = parseSectorMs(secs[i] && secs[i].value);
                if (ms != null && (best[i] == null || ms < best[i])) best[i] = ms;
            }
        }
        return best;
    }

    function bestLapCell(num) {
        const e = state.driverData[num] || {};
        const t = state.timing[num] || {};
        // Don't surface an out/in/stopped lap as a "best lap" (can happen
        // when it's the only completed lap and F1 flags it personal-best).
        const bt = lapTypeAt(num, e.bestLapNum);
        if (bt === 'OUT' || bt === 'PIT' || bt === 'STOP') {
            return `<span class="lap-time lap-empty">--:--.---</span>`;
        }
        const txt = e.bestLap || t.bestLapTime || '';
        let cls = 'lap-empty';
        if (txt) {
            const ms = parseLapMs(txt);
            const isFastest = ms != null && ms === state.overallBestLapMs;
            if (IS_RACE) {
                // Race: only the fastest is purple; everyone else by pace colour.
                if (isFastest) cls = 'lap-purple';
                else {
                    const pc = (state.driverData[num] || {}).paceColour;
                    cls = pc ? `lap-pace-${pc}` : 'lap-pace-white';
                }
            } else if (IS_QUALI) {
                // Quali: eliminated → white; fastest → purple; elimination-zone
                // (gapIsRed) → red; else Δ-to-P1 bands capped at orange (no red).
                if (state.eliminated && state.eliminated.has(num)) cls = 'lap-white';
                else if (isFastest) cls = 'lap-purple';
                else if ((state.driverData[num] || {}).gapIsRed) cls = 'band-red';
                else cls = (ms != null && state.overallBestLapMs != null
                            ? bandClass(ms - state.overallBestLapMs, true) : null) || 'band-orange';
            } else {
                // Practice: standard purple(fastest)/green(PB)/yellow — no bands.
                if (state.overallBestLapDriver === num) cls = 'lap-purple';
                else if (e.bestLapPersonal) cls = 'lap-green';
                else cls = 'lap-yellow';
            }
        }
        return `<span class="lap-time ${cls}">${txt || '--:--.---'}</span>`;
    }

    function gapCellEmpty() {
        return '<span class="gap gap-empty">--.---</span>';
    }

    function isSlowLapClass(num) {
        // Cool-down (SLOW) only. OUT no longer suppresses — out-lap times and
        // sectors are shown now (card 81 reverses the old OUT-lap suppression).
        const cls = state.lapCls[num] && state.lapCls[num].status;
        return cls === 'SLOW';
    }

    // Per-lap classification type (from driverLapClassification). Used to
    // suppress rendering a lap TIME for out/in/stopped laps — those aren't
    // representative timed laps even though F1 reports a time for them.
    function lapTypeAt(num, lapNum) {
        return lapNum != null ? (state.lapClsByLap[num] || {})[lapNum] : undefined;
    }

    // Pick the lap object whose data we should display in the last-lap +
    // sector cells. Rules:
    //   - if the current lap's classification is COOL/OUT/ABORT, NEVER
    //     show current-lap data — fall back to the previous fast lap if
    //     we have one, otherwise return null (callers render placeholders)
    //   - while the new lap hasn't reached sector 1 yet, keep showing the
    //     previous lap so the row doesn't blank out for ~30s
    //   - otherwise use the live current lap
    // Returns null if nothing is appropriate to display.
    function chooseLapForDisplay(num) {
        const t = state.timing[num];
        if (isSlowLapClass(num)) {
            return state.prevFastLap[num] || null;
        }
        if (!t || !state.sectorsCleared[num]) {
            return state.prevFastLap[num] || state.prevLap[num] || t || null;
        }
        return t;
    }

    // Retired drivers (= status RET / STOP / DSQ): clear last-lap +
    // sector + mini-sector cells. Race retirements stop accumulating
    // useful data; the row should keep only persistent identity (= rank,
    // best lap, tyre history). Applies to race only — in P/Q a "RET"
    // state during the session isn't a final retirement.
    function isRetired(num) {
        if (!IS_RACE) return false;
        const st = state.status[num];
        return st === 'RET' || st === 'STOP' || isDriverDSQ(num);
    }

    function lastLapCell(num) {
        if (isRetired(num)) {
            return `<span class="lap-time lap-last lap-empty">--:--.---</span>`;
        }
        // Quali: an eliminated driver keeps their best lap but the last-lap cell
        // is cleared — they're done running in the part they were knocked out of.
        if (!IS_RACE && state.eliminated && state.eliminated.has(num)) {
            return `<span class="lap-time lap-last lap-empty">--:--.---</span>`;
        }
        // Spec depends on session type:
        //   - PRACTICE / QUALIFYING: show the actual lap time including
        //     in-pit and out laps (card 81); cool-down (SLOW) falls back to
        //     the previous fast lap so a cool lap doesn't read as pace.
        //   - RACE: ALWAYS show the latest lap data including IN/OUT,
        //     because every lap matters for race position + gap, and
        //     IN/OUT laps still produce times the engineer cares about.
        const cur = state.timing[num];
        const prevFast = state.prevFastLap[num];
        const prevAny = state.prevLap[num];
        const cleared = state.sectorsCleared[num];   // S1 of current lap seen
        const slow = isSlowLapClass(num);             // COOL/ABORT/OUT

        let source;
        if (IS_RACE) {
            // Race: prefer the current lap's lap-time once it lands,
            // otherwise fall back to the most recent previous lap so
            // the row doesn't blank out mid-lap.
            if (cur && cur.lapTime) source = cur;
            else source = prevAny || cur;
        } else {
            // P/Q: in-pit and out laps now show their actual time (card 81);
            // cool-down (SLOW) still falls back to the previous fast lap, and
            // the !cleared fallback avoids a ~30s blank between laps.
            if (slow) {
                source = prevFast;
            } else if (!cleared) {
                source = prevFast;
            } else if (cur && cur.lapTime) {
                source = cur;
            } else {
                source = prevFast;
            }
        }

        // An out lap / stopped "lap" isn't a representative timed lap —
        // suppress its time even though F1 reports one. (In-pit laps in race
        // are intentionally kept per the source-selection above.)
        // A stopped car has no representative time. (OUT laps are now shown —
        // card 81 — so only STOP is blanked here.)
        const st = source ? lapTypeAt(num, source.lap) : undefined;
        if (st === 'STOP') {
            return `<span class="lap-time lap-last lap-empty">--:--.---</span>`;
        }
        const last = (source && source.lapTime) || '';
        let cls = 'lap-empty';
        if (last) {
            if (IS_RACE) {
                // Race: colour by pace vs the leader's reference lap (paceColour).
                const pc = (state.driverData[num] || {}).paceColour;
                cls = pc ? `lap-pace-${pc}` : 'lap-pace-white';
            } else if (IS_QUALI) {
                // Quali: the last lap is NOT colour-coded (only the best lap is).
                cls = 'lap-plain';
            } else {
                // Practice: standard purple(fastest)/green(PB)/yellow.
                const lastMs = parseLapMs(last);
                if (state.overallBestLapMs != null && lastMs != null
                        && lastMs === state.overallBestLapMs) cls = 'lap-purple';
                else if (source.personalFastest) cls = 'lap-green';
                else cls = 'lap-yellow';
            }
        }
        return `<span class="lap-time lap-last ${cls}">${last || '--:--.---'}</span>`;
    }

    function lastVsBestCell(num) {
        const lap = chooseLapForDisplay(num);
        const t = state.timing[num] || {};
        const lastMs = parseLapMs(lap && lap.lapTime);
        const bestMs = parseLapMs(t.bestLapTime);
        if (lastMs == null || bestMs == null) {
            return '<span class="delta delta-empty">+-.---</span>';
        }
        const diff = lastMs - bestMs;
        let cls = 'delta-yellow';
        if (state.overallBestLapMs != null && lastMs === state.overallBestLapMs) cls = 'delta-purple';
        else if (diff <= 0) cls = 'delta-green';
        return `<span class="delta ${cls}">${formatGap(diff)}</span>`;
    }

    function sectorCells(num) {
        if (isRetired(num)) return emptySectorCells();
        // Race: always show the most recent sectors, including IN/OUT
        // laps (= every lap matters for race-engineer view). P/Q: only
        // show prev FAST sectors, hide PIT/OUT/IN/COOL.
        const cur = state.timing[num];
        const prevFast = state.prevFastLap[num];
        const prevAny = state.prevLap[num];
        const cleared = state.sectorsCleared[num];
        const slow = isSlowLapClass(num);

        let lap;
        if (IS_RACE) {
            // Always show the current lap's sectors in race. No
            // gating on `cleared` (= the "S1 of new lap seen" flag),
            // which can be stale across the race-start reset; F1's
            // own data for the current lap is authoritative.
            lap = cur;
        } else {
            // In-pit and out laps now show their sectors (card 81); cool-down
            // (SLOW) still falls back to the previous fast lap.
            if (slow) lap = prevFast;
            else if (cleared) lap = cur;
            else lap = prevFast;
        }

        // Race-mode sector display rules:
        //   Pre-S1-of-new-lap (= !cleared): show the PREVIOUS lap's
        //     full S1/S2/S3 so the row never blanks across the new-lap
        //     reset that wipes state.timing[num].sectors.
        //   Post-S1 (= cleared): show CURRENT lap's sectors only.
        //     S2/S3 of the previous lap get cleared as S1 of the new
        //     lap arrives (= per SME 2026-06-07).
        let sectors = (lap && lap.sectors) || [{}, {}, {}];
        if (IS_RACE && !cleared && prevAny && prevAny.sectors) {
            sectors = prevAny.sectors;
        }
        // Sector colouring:
        //   Quali → PLAIN (current sectors not colour-coded; only best sectors are).
        //   Race  → fastest sector purple, else Δ-to-fastest bands (lenient).
        //   Practice → standard purple(fastest)/green(PB)/yellow.
        const fastest = IS_RACE ? fastestSectors() : null;
        const out = [];
        for (let i = 0; i < 3; i++) {
            const s = sectors[i] || {};
            const v = s.value || '';
            let cls = 'sector-empty';
            if (v) {
                if (IS_QUALI) {
                    cls = 'sector-plain';
                } else if (IS_RACE) {
                    const ms = parseSectorMs(v);
                    if (ms != null && fastest[i] != null && ms === fastest[i]) cls = 'sector-purple';
                    else cls = (ms != null && fastest[i] != null
                                ? bandClass(ms - fastest[i], false) : null) || 'sector-yellow';
                } else {
                    if (s.overallFastest) cls = 'sector-purple';
                    else if (s.personalFastest) cls = 'sector-green';
                    else cls = 'sector-yellow';
                }
            }
            out.push(`<span class="sector-time ${cls}">${v || '--.---'}</span>`);
        }
        return out.join('');
    }

    function segmentBarsCell(num) {
        if (isRetired(num)) return emptySegmentsHtml();
        // Driver has taken the chequered flag — they're done; mini-
        // sector activity no longer applies. Race + Q both: once
        // they've crossed S/F under chequered, blank the bars.
        if (isFinished(num)) {
            return emptySegmentsHtml();
        }
        // Mini-sectors render coloured only for the live fast lap. On a
        // cool / out / abort lap (or before any data arrives) we draw all
        // bars uncoloured so the row doesn't pretend the driver is on a
        // push. Layout still draws the same number of bars so column
        // widths stay aligned. Race-mode bypass: ALWAYS show colours in
        // race (= every lap is a race lap from the engineer's view).
        const showColours = IS_RACE || !isSlowLapClass(num);
        const t = state.timing[num] || {};
        let sectors = showColours ? (t.sectors || []) : [];
        // Race-mode merge: same as sectorCells — fall back to the
        // previous lap's segment list per-slot so S2/S3 mini-sector
        // bars don't blank out at every new-lap reset.
        if (IS_RACE && showColours) {
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
                const cls = seg ? (SEGMENT_COLOR_CLASS[seg] || 'seg-empty') : 'seg-empty';
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
        // Hide in the pits (status authority — lapCls can lag the transition).
        // Applies to both the live prediction and the observed result.
        if (state.status[num] === 'PIT') {
            return '<span class="pred"></span>';
        }
        // The LIVE prediction (observed=false) renders only during a PUSH lap
        // with a reference lap. The OBSERVED result (observed=true) is the just-
        // completed lap's actual outcome and shows regardless of current class
        // (cards 62/67).
        if (!p.observed) {
            if ((state.lapCls[num] || {}).status !== 'PUSH') {
                return '<span class="pred"></span>';
            }
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
        if (!txt) return '<span class="gap gap-empty">+-.---</span>';
        // Eliminated (quali): gap is the frozen bubble gap, shown white.
        if (state.eliminated && state.eliminated.has(num)) {
            return `<span class="gap gap-white">${txt}</span>`;
        }
        // Elimination zone → red (red is reserved for the zone only).
        if (e.gapIsRed) return `<span class="gap gap-red">${txt}</span>`;
        if (IS_QUALI) {
            // Gap = Δ to P1 → bands, capped at orange (big gaps stay orange; red
            // is reserved for the elimination zone). (P/Q only; race uses
            // gapOrLapForRaceP1.)
            const gms = txt.includes(':') ? parseLapMs(txt)
                      : Math.round(parseFloat(txt.replace('+', '')) * 1000);
            const band = bandClass(gms, true);
            return `<span class="gap ${band || ''}">${txt}</span>`;
        }
        // Practice: plain (no bands).
        return `<span class="gap">${txt}</span>`;
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

    function lapCountClass(num) {
        // Race only: colour the lap counter by laps-down from the leader.
        //   yellow — a full lap or more down (gap shows laps, e.g. "+1 LAP")
        //   white  — 1 lap behind on the counter but gap is still a time
        //   green  — same lap as the leader (default)
        if (!IS_RACE) return '';
        const n = (state.timing[num] || {}).lap || 0;
        // Leader lap = the highest per-driver lap — SAME source as `n`
        // (driverLaps.currentLap). Using raceLaps (state.currentLap) here misreads
        // the leader as 1-down, since raceLaps.currentLap can lead t.lap by one.
        let leaderLap = 0;
        for (const k in state.timing) {
            const l = state.timing[k].lap || 0;
            if (l > leaderLap) leaderLap = l;
        }
        const gap = ((state.driverData[num] || {}).gap || '').toUpperCase();
        if (gap.includes('L')) return ' lap-count-yellow';
        if (n && leaderLap && n < leaderLap) return ' lap-count-white';
        return ' lap-count-green';
    }

    function lapCountCell(num) {
        const t = state.timing[num] || {};
        // Lap count = the authoritative NoL-based current lap
        // (driverLaps.currentLap, stored as t.lap). NOT max(lapTimes key):
        // in P/Q the highest TIMED lap is currentLap-1, so once the first lap
        // time arrived the count regressed (e.g. COL 1→2→1, I10). currentLap is
        // monotonic and already session-correct.
        const n = t.lap || 0;
        return `<span class="lap-count${lapCountClass(num)}">${n || '0'}</span>`;
    }

    function gapOrLapForRaceP1(num, position) {
        if (position === 1) {
            return `<span class="gap p1-lap">L${state.currentLap || ''}</span>`;
        }
        const e = state.driverData[num] || {};
        const t = e.gapTrend === 'green' ? ' gap-trend-green'
                : e.gapTrend === 'yellow' ? ' gap-trend-yellow' : '';
        return `<span class="gap${t}">${e.gap || ''}</span>`;
    }

    function intervalCell(num) {
        const e = state.driverData[num] || {};
        const t = e.intTrend === 'green' ? ' int-trend-green'
                : e.intTrend === 'yellow' ? ' int-trend-yellow' : '';
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
        cols += `<span class="rank">${idx + 1}</span>`;
        cols += `<span class="driver-color" style="--team-color:${drv.color}"></span>`;
        cols += `<span class="driver-tla">${drv.tla}</span>`;

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
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += sectorCells(num);
            cols += lastLapCell(num);
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
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += sectorCells(num);
            cols += lastLapCell(num);
            cols += predictionCell(num);
            cols += `<span class="tyres">${tyreCell(num, true)}</span>`;
        } else {
            // Practice
            const stt = statusCell(num);
            cols += `<span class="status ${stt.cls}">${stt.text}</span>`;
            cols += bestLapCell(num);
            cols += gapCell(num);
            cols += `<span class="segments">${segmentBarsCell(num)}</span>`;
            cols += sectorCells(num);
            cols += lastLapCell(num);
            cols += `<span class="tyres">${tyreCell(num, false)}</span>`;
        }

        // Laps column + trailing spacer — all session types. Quali shows
        // the current tyre in a narrower tyre column but still shows the
        // lap count; each grid template includes the matching Laps +
        // laps-end-spacer tracks.
        cols += lapCountCell(num);
        cols += '<span class="col-spacer"></span>';

        // Driver colour + TLA mirrored at the END of the row so each
        // driver is easy to identify regardless of how wide / scrolled
        // the row is.
        cols += `<span class="driver-color driver-color-end" style="--team-color:${drv.color}"></span>`;
        cols += `<span class="driver-tla driver-tla-end">${drv.tla}</span>`;

        return `<div class="${cls.join(' ')}" data-driver="${num}">${cols}</div>`;
    }

    // Header — column count must match the row's grid template for that
    // session type (see standings.css). Driver-identification cells get
    // no header text (rank/colour/tla/status are self-explanatory).
    function buildHeader() {
        // Header order MUST match buildRow's column order.
        // Canonical: ... | Mini | S1 | S2 | S3 | Lap time | <tail>
        // Common left identifier-block header (= rank empty, "Driver"
        // spanning colour+tla, status empty). Explicit grid-column on
        // the Driver span keeps subsequent spans flowing into col 4+.
        const idHdr =
            '<span></span>' +                                        /* rank */
            '<span style="grid-column: 2 / span 2">Driver</span>' +  /* color + tla */
            '<span></span>';                                         /* status */

        if (IS_RACE) {
            return (
                '<div class="driver-header">' +
                idHdr +
                '<span>Gap</span>' +
                '<span></span>' + /* gap-int-spacer */
                '<span>Int</span>' +
                '<span>Mini-sectors</span>' +
                '<span>S1</span>' +
                '<span>S2</span>' +
                '<span>S3</span>' +
                '<span>Lap time</span>' +
                '<span>Best lap</span>' +
                '<span class="col-spacer"></span>' +
                '<span>Tyres</span>' +
                '<span>Laps</span>' +
                '<span></span>' + /* laps-end-spacer */
                '<span></span><span></span>' + /* color-end + tla-end */
                '</div>'
            );
        }
        if (IS_QUALI) {
            return (
                '<div class="driver-header">' +
                idHdr +
                '<span>Best lap</span>' +
                '<span>Gap</span>' +
                '<span>Mini-sectors</span>' +
                '<span>S1</span>' +
                '<span>S2</span>' +
                '<span>S3</span>' +
                '<span>Lap time</span>' +
                '<span>Delta</span>' +
                '<span>Tyre</span>' +
                '<span>Laps</span>' +
                '<span></span>' + /* laps-end-spacer */
                '<span></span><span></span>' + /* color-end + tla-end */
                '</div>'
            );
        }
        // Practice.
        return (
            '<div class="driver-header">' +
            idHdr +
            '<span>Best lap</span>' +
            '<span>Gap</span>' +
            '<span>Mini-sectors</span>' +
            '<span>S1</span>' +
            '<span>S2</span>' +
            '<span>S3</span>' +
            '<span>Lap time</span>' +
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
