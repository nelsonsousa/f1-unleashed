/**
 * Unified Standings Tile
 *
 * One component for practice, qualifying, and race. Renders columns per
 * the spec from the user; session_type drives which columns are shown.
 *
 * Order is the classification order from the server (display:standings).
 *
 * Subscriptions:
 *   driverList               — TLA + team colour
 *   display:standings        — sorted positions, bestLap, gap, penalties,
 *                              currentLap (race), eliminated (qualifying)
 *   driverTiming:NN          — current lap sectors+segments, lastLap,
 *                              bestLap, total laps
 *   driverTyres:NN           — tyre stints array (compound, isNew, laps)
 *   driverStatus:NN          — PIT / OUT / TRACK / RET / STOP
 *   lapClassification:NN     — PUSH / COOL / OUT / IN / RACE / ABORT
 *   lapPrediction:NN         — predicted lap time + position (qualifying)
 *   sessionInfo              — qualifyingPart for knockout-zone gap
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
        standingsOrder: [],   // [num, num, ...] from display:standings
        driverData: {},       // num → entry from display:standings (gap, bestLap, penalties, etc.)
        timing: {},           // num → driverTiming (current lap)
        prevLap: {},          // num → snapshot of last completed lap (lapTime + sectors)
        prevFastLap: {},      // num → snapshot of last non-cool lap
        sectorsCleared: {},   // num → bool: have we cleared prev-lap sectors yet
        tyres: {},            // num → tyre stints array
        lapTimes: {},         // num → {lap → time_str} from driverLapTimes
        status: {},           // num → PIT/OUT/TRACK/RET/STOP
        lapCls: {},           // num → {lap, status} (latest classification)
        lapClsByLap: {},      // num → {lapNum → status} per-lap map
        prediction: {},       // num → lapPrediction
        currentLap: 0,        // race-only
        qualifyingSegment: null,
        eliminated: new Set(),
        // Chequered-flag tracking. `chequeredOut` flips true when the
        // CHEQUERED FLAG RC message arrives. Every driverLastLap after
        // that adds the driver to `finishedDrivers` — they crossed S/F
        // under the chequered = took the flag. Works uniformly for
        // P/Q (per Q segment) and race.
        chequeredOut: false,
        finishedDrivers: new Set(),
        // Track the overall-fastest lap so we can purple-tint a personal best
        // that beats it.
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
        const e = state.driverData[num];
        return !!(e && e.finished);
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
    let fiaStewardsStack = [];

    messageBus.on('fiaStewards', (data) => {
        if (!data || !Array.isArray(data.stack)) return;
        fiaStewardsStack = data.stack;
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
        if (!fiaStewardsStack.length) return [];
        const nowMs = messageBus.getCurrentOffset
            ? messageBus.getCurrentOffset() * 1000 : 0;
        const matches = [];
        for (const e of fiaStewardsStack) {
            if (!Array.isArray(e.driverNums)) continue;
            if (!e.driverNums.includes(num)) continue;
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
        for (const e of fiaStewardsStack) {
            if (e.kind === 'blackFlag'
                    && Array.isArray(e.driverNums)
                    && e.driverNums.includes(num)) return true;
        }
        return false;
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

    messageBus.on('display:standings', (data) => {
        if (!data || !Array.isArray(data.drivers)) return;
        state.standingsOrder = data.drivers.map(e => e.num);
        for (const e of data.drivers) {
            state.driverData[e.num] = e;
            const d = ensureDriver(e.num);
            if (e.tla) d.tla = e.tla;
            if (e.color) d.color = e.color;
            if (e.team) d.team = e.team;
            if (e.knockedOut) state.eliminated.add(e.num);
        }
        if (data.currentLap != null) state.currentLap = data.currentLap;
        if (data.qualifyingSegment != null) state.qualifyingSegment = data.qualifyingSegment;
        render();
    });

    messageBus.on('driverTiming:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;

        const prev = state.timing[num];

        // Snapshot the just-completed lap when its lap number is replaced
        // by the next lap. Keyed on lap-number change (not LastLapTime
        // arrival) so the snapshot still happens when LastLapTime lands
        // in a separate patch from the rollover. `prev` is captured —
        // it holds the just-completed lap's final state with whatever
        // sectors and lapTime arrived for it.
        const lapChanged = prev && prev.lap > 0
            && data.lap && data.lap !== prev.lap;
        if (lapChanged) {
            const snap = {
                lap: prev.lap,
                lapTime: prev.lapTime,
                overallFastest: prev.overallFastest,
                personalFastest: prev.personalFastest,
                sectors: prev.sectors ? prev.sectors.map(s => ({ ...s })) : null,
            };
            state.prevLap[num] = snap;
            // Per-lap classification (NOT latest) — the just-completed
            // lap's classification is what determines whether it counts
            // as the new "last fast lap".
            const prevCls = (state.lapClsByLap[num] || {})[prev.lap];
            if (!prevCls
                    || (prevCls !== 'COOL' && prevCls !== 'ABORT'
                        && prevCls !== 'OUT' && prevCls !== 'IN')) {
                state.prevFastLap[num] = snap;
            }
            state.sectorsCleared[num] = false;  // new lap starting
        }

        // Detect first new sector arrival → tear down the previous-lap
        // sector overlay so we render only the current lap's cells from
        // here on (rule: "as soon as the first new sector time arrives,
        // clear all previous lap sector times").
        const newSector1 = data.sectors && data.sectors[0] && data.sectors[0].value;
        if (newSector1 && !state.sectorsCleared[num]) {
            state.sectorsCleared[num] = true;
        }

        state.timing[num] = data;

        // Overall-fastest purple holder: F1 sets `overallFastest=true`
        // on a lap at the moment it becomes the new session-best, and
        // never demotes. So whenever we see that flag, the emitting
        // driver becomes the new purple holder — drop any prior
        // holder and assign here. The old `<`-recompute over
        // driverData picked stale Q1 best laps at Q2 start because
        // eliminated drivers' bestLap remained in state with their
        // Q1 overall-fastest flag.
        if (data.overallFastest === true) {
            state.overallBestLapDriver = num;
            state.overallBestLapMs = parseLapMs(data.lapTime || data.bestLapTime);
        }
        render();
    });

    messageBus.on('driverLapTimes:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data || typeof data !== 'object') return;
        state.lapTimes[num] = data;
        render();
    });

    messageBus.on('driverTyres:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !Array.isArray(data)) return;
        state.tyres[num] = data;
        render();
    });

    messageBus.on('driverStatus:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        state.status[num] = data;
        // Driver transitioned into a passive state AFTER chequered
        // (= e.g. cool lap → pit) — they're done.
        markDriverFinishedIfPassive(num);
        render();
    });

    // BUG fix (2026-06-06): the processor emits TWO classifications at
    // lap boundaries — (a) forward "PUSH" for the new lap N+1, then (b)
    // the finalized "COOL/OUT" for the just-ended lap N. If we let (b)
    // overwrite `lapCls`, the current-lap indicator regresses to the
    // OLD lap's status while the driver is already on the new lap.
    // Fix: gate `lapCls` updates to `data.lap >= state.lapCls.lap`.
    // `lapClsByLap` still gets every per-lap update so historical
    // lookups (= Delta cell, pace, predictions) remain accurate.
    messageBus.on('lapClassification:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        // Gate `lapCls` (= "current lap" indicator) so a retroactive
        // finalize emit for an OLDER lap doesn't regress the current-
        // lap status. Only accept emits whose lap >= what we already
        // believe is the current lap.
        const curr = state.lapCls[num] || { lap: 0 };
        if (data.lap != null && data.lap >= (curr.lap || 0)) {
            state.lapCls[num] = { lap: data.lap, status: data.status };
        }
        // Per-lap map — used by snapshot/Delta lookups. MERGE rather
        // than replace, so finalize emits enrich history instead of
        // wiping it.
        let map = state.lapClsByLap[num] || {};
        if (data.laps && typeof data.laps === 'object') {
            // Snapshot payload (= restore on seek) — replace wholesale.
            map = {};
            for (const [lap, status] of Object.entries(data.laps)) {
                map[parseInt(lap)] = status;
            }
        }
        if (data.lap != null) map[data.lap] = data.status;
        state.lapClsByLap[num] = map;
        // After chequered, a lap classification flip into a passive
        // state (= PUSH → COOL, OUT → IN, …) means the driver is done.
        markDriverFinishedIfPassive(num);
        render();
    });

    // Server-emitted snapshot of the most recently completed lap. Used
    // on restore/seek when the latest driverTiming is the empty post-
    // rollover emit and the client has no live prev to snapshot from.
    messageBus.on('driverLastLap:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num || !data) return;
        const snap = {
            lap: data.lap,
            lapTime: data.lapTime,
            overallFastest: data.overallFastest,
            personalFastest: data.personalFastest,
            sectors: data.sectors ? data.sectors.map(s => ({ ...s })) : null,
        };
        state.prevLap[num] = snap;
        const cls = (state.lapClsByLap[num] || {})[data.lap];
        if (!cls
                || (cls !== 'COOL' && cls !== 'ABORT'
                    && cls !== 'OUT' && cls !== 'IN')) {
            state.prevFastLap[num] = snap;
        }
        // A fresh lap-time AFTER the CHEQUERED FLAG = driver just
        // crossed S/F under chequered = they've taken the flag.
        if (state.chequeredOut) {
            state.finishedDrivers.add(num);
        }
        render();
    });

    // CHEQUERED FLAG arrival = enter "taking the flag" mode. Each Q
    // segment fires its own CHEQUERED → wipe prior finishers so Q2 / Q3
    // start clean. At the moment of CHEQUERED, mark every driver who
    // is already DONE (= in PIT, on a COOL/OUT/IN lap) — they aren't
    // attacking and won't cross S/F on a fresh PUSH. Drivers on an
    // active PUSH/LONG lap get marked when their lap-time arrives via
    // the driverLastLap handler.
    function markDriverFinishedIfPassive(num) {
        if (!state.chequeredOut) return;
        const st = state.status[num];
        const cls = (state.lapCls[num] || {}).status;
        if (st === 'PIT' || st === 'RET' || st === 'STOP'
                || cls === 'COOL' || cls === 'OUT' || cls === 'IN'
                || cls === 'PIT' || cls === 'ABORT') {
            state.finishedDrivers.add(num);
        }
    }
    // Track how many CHEQUERED FLAG messages we've SEEN — fire the
    // chequered-trigger only when a NEW one arrives, not on every
    // raceControlMessages re-broadcast (= each new RC re-emits the
    // full list, so iterating would keep re-firing old Q1 chequered
    // even after the Q2 segment reset wiped finishedDrivers).
    let _prevChequeredCount = 0;
    messageBus.on('raceControlMessages', (data) => {
        if (!Array.isArray(data)) return;
        const count = data.filter((msg) => {
            const text = (msg && msg.message) || '';
            return /CHEQUERED FLAG/i.test(text) && !/BLACK AND WHITE/i.test(text);
        }).length;
        if (count > _prevChequeredCount) {
            if (!state.chequeredOut) {
                state.finishedDrivers = new Set();
            }
            state.chequeredOut = true;
            for (const num of Object.keys(state.driverData)) {
                markDriverFinishedIfPassive(num);
            }
        }
        _prevChequeredCount = count;
        render();
    });

    messageBus.on('lapPrediction:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        state.prediction[num] = data || {};
        render();
    });

    messageBus.on('sessionInfo', (data) => {
        if (!data) return;
        if (data.qualifyingPart) {
            const nextSeg = `Q${data.qualifyingPart}`;
            if (state.qualifyingSegment && state.qualifyingSegment !== nextSeg) {
                // A new Q segment is starting — wipe the chequered-flag
                // indicators from the prior segment. Q1's finishers are
                // not Q2's finishers; the indicator must reset.
                state.chequeredOut = false;
                state.finishedDrivers = new Set();
                // Also wipe the overall-fastest purple holder. The
                // prior segment's best is no longer the session-best
                // (= Q2 / Q3 start their own classification); waiting
                // for F1 to re-emit overallFastest=true in the new
                // segment is correct.
                state.overallBestLapDriver = null;
                state.overallBestLapMs = null;
            }
            state.qualifyingSegment = nextSeg;
            render();
        }
    });

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
        state.status = {};
        state.lapCls = {};
        state.lapClsByLap = {};
        state.prediction = {};
        state.currentLap = 0;
        state.qualifyingSegment = null;
        state.eliminated = new Set();
        state.overallBestLapMs = null;
        state.overallBestLapDriver = null;
        state.chequeredOut = false;
        state.finishedDrivers = new Set();
        render();
    });

    // ─── Render ───

    // Practice / qualifying only: when the driver isn't on a flying lap
    // (out lap, cool-down, aborted, in-pit, etc.) the current-lap timing
    // data is irrelevant. Hide sectors / last-lap / Δ / mini-segments so
    // the row only shows persistent info (best lap, gap, tyres).
    const SLOW_CLASSIFICATIONS = new Set(['OUT', 'COOL', 'ABORT', 'IN']);
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
    const CHEQUERED_SVG = '<svg class="st-chequered-svg" width="16" height="14" viewBox="0 0 16 14" stroke="rgba(0,0,0,0.7)" stroke-width="0.5">'
        + '<rect width="16" height="14" fill="white"/>'
        + '<rect x="0"  y="0"  width="4" height="3.5" fill="black"/>'
        + '<rect x="8"  y="0"  width="4" height="3.5" fill="black"/>'
        + '<rect x="4"  y="3.5" width="4" height="3.5" fill="black"/>'
        + '<rect x="12" y="3.5" width="4" height="3.5" fill="black"/>'
        + '<rect x="0"  y="7"  width="4" height="3.5" fill="black"/>'
        + '<rect x="8"  y="7"  width="4" height="3.5" fill="black"/>'
        + '<rect x="4"  y="10.5" width="4" height="3.5" fill="black"/>'
        + '<rect x="12" y="10.5" width="4" height="3.5" fill="black"/>'
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
                const s = lc.status;
                base = (s === 'ABORT')
                    ? { text: 'ABRT', cls: 'st-abort' }
                    : { text: s, cls: `st-${s.toLowerCase()}` };
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

    function bestLapCell(num) {
        const e = state.driverData[num] || {};
        const t = state.timing[num] || {};
        const txt = e.bestLap || t.bestLapTime || '';
        let cls = 'lap-empty';
        if (txt) {
            if (IS_RACE) {
                // Race: recompute the overall fastest from all drivers'
                // bestLap. F1 doesn't push a reliable demotion signal
                // and retroactive lap-time changes shuffle the leader,
                // so the data-driven flag-tracking path used for P/Q
                // is too lossy here. 20-driver sweep per cell is cheap.
                const ms = parseLapMs(txt);
                let overallBest = Infinity;
                for (const n of Object.keys(state.driverData)) {
                    const od = state.driverData[n] || {};
                    const ot = state.timing[n] || {};
                    const m = parseLapMs(od.bestLap || ot.bestLapTime);
                    if (m != null && m < overallBest) overallBest = m;
                }
                if (ms != null && ms === overallBest) cls = 'lap-purple';
                else if (e.bestLapPersonal) cls = 'lap-green';
                else cls = 'lap-yellow';
            } else {
                // P/Q: trust F1's overallFastest flag tracking. Single
                // purple holder maintained in state.overallBestLapDriver
                // and reset on Q segment change.
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
        const cls = state.lapCls[num] && state.lapCls[num].status;
        return cls === 'COOL' || cls === 'ABORT' || cls === 'OUT';
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
        // Spec depends on session type:
        //   - PRACTICE / QUALIFYING: only show prev FAST laps. Hide
        //     OUT/IN/COOL/PIT data — it's not representative of pace.
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
            // P/Q: hide unless prev was a FAST lap.
            if (state.status[num] === 'PIT') {
                return `<span class="lap-time lap-last lap-empty">--:--.---</span>`;
            }
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

        const last = (source && source.lapTime) || '';
        let cls = 'lap-empty';
        if (last) {
            // overallFastest in the snapshot is frozen at the moment the
            // lap completed — F1 doesn't re-emit demotions. Compute the
            // purple tint live by comparing against the running overall
            // best so a beaten lap drops to green / yellow.
            const lastMs = parseLapMs(last);
            if (state.overallBestLapMs != null
                    && lastMs != null
                    && lastMs === state.overallBestLapMs) {
                cls = 'lap-purple';
            } else if (source.personalFastest) {
                cls = 'lap-green';
            } else {
                cls = 'lap-yellow';
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
            if (state.status[num] === 'PIT') return emptySectorCells();
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
        const out = [];
        for (let i = 0; i < 3; i++) {
            const s = sectors[i] || {};
            const v = s.value || '';
            let cls = 'sector-empty';
            if (v) {
                if (s.overallFastest) cls = 'sector-purple';
                else if (s.personalFastest) cls = 'sector-green';
                else cls = 'sector-yellow';
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
        const p = state.prediction[num];
        if (!p || p.delta_s === undefined || p.delta_s === null) {
            return '<span class="pred"></span>';
        }
        // Hide delta when the driver is in PIT (= status authority);
        // state.lapCls may lag behind the live driverStatus transition,
        // so we check both. Delta only renders for an active PUSH lap.
        if (state.status[num] === 'PIT') {
            return '<span class="pred"></span>';
        }
        const cls = (state.lapCls[num] || {}).status;
        if (cls !== 'PUSH') {
            return '<span class="pred"></span>';
        }
        const driverEntry = state.driverData[num] || {};
        const hasReference = Boolean(driverEntry.bestLap)
            || (state.lapTimes[num]
                && Object.keys(state.lapTimes[num]).length > 0);
        if (!hasReference) {
            return '<span class="pred"></span>';
        }
        const delta = p.delta_s;
        const sign = delta < 0 ? '−' : '+';
        const deltaText = `${sign}${Math.abs(delta).toFixed(1)}`;
        const deltaCls = delta < 0 ? 'pred-delta-neg' : 'pred-delta-pos';

        // Predicted position computed client-side (= depends on every
        // OTHER driver's session-best lap; the processor doesn't know
        // about them). Sort all known session bests + this driver's
        // predicted lap, find this driver's rank. Per SME 2026-06-07:
        // suppress projection when delta is positive (= driver is on
        // course to be SLOWER than their best, no actual improvement
        // to predict a position from).
        let posHtml = '';
        if (p.predictedTimeMs && delta < 0) {
            const projected = computePredictedPosition(num, p.predictedTimeMs);
            if (projected) {
                const isP1 = projected === 1;
                const posCls = isP1 ? 'pred-pos-p1' : 'pred-pos';
                posHtml = `<span class="${posCls}">P${projected}</span>`;
            }
        }
        return `<span class="pred">`
            + `<span class="${deltaCls}">${deltaText}</span>`
            + posHtml
            + `</span>`;
    }

    function computePredictedPosition(num, predictedMs) {
        // Build list of (driver, best lap_ms) for everyone EXCEPT the
        // current driver, then insert this driver's predicted time and
        // sort to find their projected rank.
        const others = [];
        for (const otherNum in state.driverData) {
            if (otherNum === num) continue;
            const bestStr = (state.driverData[otherNum] || {}).bestLap;
            const ms = bestStr && parseLapMs(bestStr);
            if (ms) others.push(ms);
        }
        others.push(predictedMs);
        others.sort((a, b) => a - b);
        const rank = others.indexOf(predictedMs) + 1;
        return rank > 0 ? rank : null;
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
        const cls = e.gapIsRed ? 'gap gap-red' : 'gap';
        return `<span class="${cls}">${txt}</span>`;
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
        const times = state.lapTimes[num];
        // Prefer the highest lap key from driverLapTimes (count of completed
        // laps). Fall back to driverTiming.lap (current lap in progress).
        let n = 0;
        if (times && typeof times === 'object') {
            for (const k of Object.keys(times)) {
                const v = parseInt(k);
                if (v > n) n = v;
            }
        }
        if (!n && t.lap) n = t.lap;
        return `<span class="lap-count">${n || '0'}</span>`;
    }

    function gapOrLapForRaceP1(num, position) {
        if (position === 1) {
            return `<span class="gap p1-lap">L${state.currentLap || ''}</span>`;
        }
        const e = state.driverData[num] || {};
        return `<span class="gap">${e.gap || ''}</span>`;
    }

    function intervalCell(num) {
        const e = state.driverData[num] || {};
        return `<span class="interval">${e.interval || ''}</span>`;
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

    function render() {
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

    // segmentLayout topic gives the actual mini-sector layout for the track
    messageBus.on('segmentLayout', (data) => {
        if (Array.isArray(data) && data.length === 3) {
            window.SEGMENT_LAYOUT = data;
            render();
        }
    });
})();
