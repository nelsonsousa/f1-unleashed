/**
 * Header Component
 *
 * Listens to processed topics from the server:
 *   sessionInfo    → meeting name, session badge, gmt offset
 *   clock          → UTC time, session time, clock status
 *   trackStatus    → GREEN, RED, SC/VSC messages, CHEQUERED
 *   session:events → scrubber event markers (full list, on load + seek)
 *   event          → GREEN / RED / SC / VSC / CHEQUERED markers during playback
 */

(function() {
    // =========================================================================
    // State
    // =========================================================================

    const state = {
        // Session info
        meetingName: '',
        sessionName: '',
        sessionType: '',
        // Race/sprint badge → live lap counter (card): the server-sent badge
        // text, current/total laps, and whether the race has started (lights
        // out). Until lights-out the badge shows R / S; after, L{cur}/{total}.
        baseBadge: '--',
        raceCurrentLap: null,
        raceTotalLaps: null,
        raceStarted: false,
        scheduledStartMs: null,// scheduled session start (UTC ms) — SYNC TO Lap 1 window
        gmtOffset: null,       // parsed timedelta in ms
        gmtOffsetStr: null,    // raw string e.g. "11:00:00"
        qualifyingPart: 0,
        isSprintQuali: false,

        // Clock
        utc: null,             // last UTC from clock message (Date) — first-frame fallback
        sessionTimeStr: null,  // raw "HH:MM:SS" from clock
        sessionTimeMs: null,   // parsed to ms
        sessionTimeFormat: null, // 'hms' or 'ms'
        clockStatus: 'pause',  // 'play' or 'pause'
        firstNonZeroSeen: false,

        // Track status
        trackStatusText: '--',
        trackStatusColor: 'white',

        // Scrubber
        duration: 0,
        offset: 0,
        events: [],

        // Audio
        audio: {
            element: null,
            startUtc: null,
            isReady: false,
            // Always start muted on session open (card 76). Firefox can block
            // autoplay until the user interacts; starting muted means the
            // unmute click is that interaction — it unmutes and unlocks audio
            // in one gesture. Not restored from a previous session.
            isMuted: true,
            volume: parseFloat(localStorage.getItem('audioVolume') ?? '80') / 100,
            offsetSeconds: 0,        // user-tunable shift (positive → audio plays later)
            mse: false,              // true → audio is an MSE SourceBuffer (seekable; never ?t=-reload)
            mseSeek: null,           // fn(targetSec): re-window the SourceBuffer around a far seek
        },

        // Animation
        clockAnimId: null,
    };

    // SYNC TO: per-lap start offset (ms), recorded as raceLaps arrive — the
    // race markers seek to these. Lap starts are immutable, so keep the first
    // (earliest) offset seen for each lap.
    const _lapOffset = {};

    // SYNC TO click → seek playback to the marker stored on the button.
    window.syncSeek = function () {
        const btn = document.getElementById('syncBtn');
        if (btn && btn._syncOffset != null && typeof seekToOffset === 'function') {
            seekToOffset(btn._syncOffset);
        }
    };

    // =========================================================================
    // Session Info
    // =========================================================================

    function handleSessionInfo(data) {
        if (!data || typeof data !== 'object') return;
        // Only gmtOffset is still consumed here (drives the clock display).
        // The event title and session badge are now server-computed and
        // arrive on their own topics (meetingName / sessionBadge).
        if (data.gmtOffset) {
            state.gmtOffsetStr = data.gmtOffset;
            state.gmtOffset = parseGmtOffset(data.gmtOffset);
        }
        // Scheduled session start (track-local StartDate → UTC via gmtOffset).
        // Drives the SYNC TO "Lap 1 from scheduled-start + 1 min" window (86BYppiU).
        if (data.startDate && state.gmtOffset != null) {
            const t = Date.parse(data.startDate.replace(/Z$/, '') + 'Z');
            if (!isNaN(t)) state.scheduledStartMs = t - state.gmtOffset;
        }
        // Lights out → switch the race/sprint badge to the live lap counter.
        if (data.sessionStatus === 'Started' && !state.raceStarted) {
            state.raceStarted = true;
            renderSessionBadge();
        }
    }

    function handleMeetingName(name) {
        const titleEl = document.getElementById('sessionTitle');
        if (titleEl) titleEl.textContent = name || 'Loading...';
    }

    function handleSessionBadge(badge) {
        state.baseBadge = badge || '--';
        renderSessionBadge();
    }

    // Race/sprint: once lights-out has happened (sessionStatus → Started) the
    // badge shows the live lap counter L{current}/{total}, refreshed each lap;
    // otherwise it shows the server-sent badge (R / S / FP1 / Q …). Mirrors the
    // P1 "L{n}" cell, which also composes the lap display client-side from the
    // raceLaps topic.
    function renderSessionBadge() {
        const badgeEl = document.getElementById('sessionBadge');
        if (!badgeEl) return;
        const sType = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
        const isRaceLike = (sType === 'race' || sType === 'sprint');
        if (isRaceLike && state.raceStarted && state.raceTotalLaps) {
            badgeEl.textContent = `L${state.raceCurrentLap || 0}/${state.raceTotalLaps}`;
        } else {
            badgeEl.textContent = state.baseBadge || '--';
        }
    }

    function parseGmtOffset(offsetStr) {
        if (!offsetStr) return null;
        const match = offsetStr.match(/^(-?)(\d+):(\d+):(\d+)$/);
        if (!match) return null;
        const sign = match[1] === '-' ? -1 : 1;
        return sign * (parseInt(match[2]) * 3600 + parseInt(match[3]) * 60) * 1000;
    }

    // =========================================================================
    // Clock
    // =========================================================================

    function handleClock(data, offset_ms) {
        if (!data || typeof data !== 'object') return;

        if (data.clockStatus) {
            state.clockStatus = data.clockStatus;
        }

        // Anchor wall-clock UTC. We prefer Extrapolating: true messages
        // because F1 keeps re-sending the same stale UTC during paused
        // or red-flag periods (which would freeze / rewind the displayed
        // Local Time). But if we have no anchor yet, accept a paused
        // message as a starting point so the clock shows *something* —
        // it will be replaced as soon as a play message arrives.
        if (data.utc && (state.clockStatus === 'play' || !state.utc)) {
            const utcStr = data.utc.replace('Z', '+00:00');
            state.utc = new Date(utcStr);
        }

        if (data.sessionTime) {
            const ms = parseTimeToMs(data.sessionTime);
            if (ms !== null) {
                if (ms === 0 && !state.firstNonZeroSeen) {
                    // Ignore initial 0:00
                } else {
                    if (!state.firstNonZeroSeen && ms > 0) {
                        state.firstNonZeroSeen = true;
                        state.sessionTimeFormat = ms > 3600000 ? 'hms' : 'ms';
                    }
                    // Anchor: at offset_ms the session clock showed ms
                    state.sessionTimeMs = ms;
                    state.sessionTimeAnchorMs = (offset_ms !== undefined) ? offset_ms : null;
                }
            }
        }

        if (state.gmtOffsetStr) {
            messageBus.gmtOffset = state.gmtOffsetStr;
        }

        startClockAnimation();
    }

    function parseTimeToMs(timeStr) {
        if (!timeStr) return null;
        const parts = timeStr.split(':');
        if (parts.length === 3) {
            return (parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2])) * 1000;
        }
        if (parts.length === 2) {
            return (parseInt(parts[0]) * 60 + parseInt(parts[1])) * 1000;
        }
        return null;
    }

    function startClockAnimation() {
        if (state.clockAnimId) return;
        state.clockAnimId = requestAnimationFrame(updateClocks);
    }

    // ── SYNC TO markers ──
    // Two buttons that seek playback to the previous / next marker. Markers are
    // context-dependent: pre/post-session → wall-clock whole minutes (hh:MM);
    // P/Q running → session-clock whole minutes (MM:ss); race → lap starts.
    function fmtHHMM(ms) { return new Date(ms).toUTCString().slice(17, 22); }
    function fmtMMSS(sec) {
        sec = Math.max(0, Math.round(sec));
        const m = Math.floor(sec / 60), s = sec % 60;
        return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    function updateSyncButtons() {
        const btn = document.getElementById('syncBtn');
        const modeEl = document.getElementById('syncToMode');
        if (!btn || !messageBus.clockTime || !messageBus.startTime) return;

        const sType = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionType) || '';
        const isRaceLike = (sType === 'race' || sType === 'sprint');
        const startMs = messageBus.startTime.getTime();
        const curOffset = messageBus.getCurrentOffset();   // seconds

        // The sync marker. Normally the PREVIOUS boundary at/before the playhead;
        // the race "Lap 1" marker is the exception — it targets lights-out and may
        // be AHEAD of the playhead (forward sync to a TV that's ahead). (86BYppiU)
        let mode = 'CLOCK', label = null, offset = null, allowForward = false;

        // Lights-out offset = the first GREEN track-status event (= the moment
        // SessionStatus becomes Started). It's in state.events from connect on a
        // replay, so Lap 1 is known even before the playhead reaches it — no
        // reliance on the pre-race LapCount keyframe.
        let lightsOutSec = null;
        if (isRaceLike && state.events) {
            const green = state.events.find((ev) => typeof ev.offset_ms === 'number'
                && String(typeof ev.data === 'string' ? ev.data
                          : (ev.data && ev.data.event) || '').toUpperCase() === 'GREEN');
            if (green) lightsOutSec = green.offset_ms / 1000;
        }
        const preRaceLap1 = isRaceLike && !state.raceStarted && state.scheduledStartMs != null
            && messageBus.clockTime.getTime() >= state.scheduledStartMs + 60000;
        const racingLap1 = isRaceLike && state.raceStarted
            && (state.raceCurrentLap == null || state.raceCurrentLap <= 1);

        if (preRaceLap1 || racingLap1) {
            // Race Lap 1: jump to lights-out. Forward-seekable — it may be ahead
            // of the playhead (the pre-race window, or a TV ahead of the data).
            mode = 'LAP';
            label = 'Lap 1';
            offset = lightsOutSec;
            allowForward = true;
        } else if (isRaceLike && state.raceStarted && state.raceCurrentLap) {
            mode = 'LAP';
            const cl = state.raceCurrentLap;   // start of the current lap
            label = `Lap ${cl}`;
            offset = _lapOffset[cl] != null ? _lapOffset[cl] / 1000 : null;
        } else if (state.clockStatus === 'play' && state.firstNonZeroSeen
                   && state.sessionTimeMs != null) {
            // P/Q running: the session-clock whole minute just crossed (the clock
            // counts DOWN, so that's slightly MORE remaining than now).
            mode = 'SESSION';
            let remMs = state.sessionTimeMs;
            if (state.sessionTimeAnchorMs != null) {
                remMs = state.sessionTimeMs - (curOffset * 1000 - state.sessionTimeAnchorMs);
            }
            const remSec = Math.max(0, remMs / 1000);
            const markSec = Math.ceil(remSec / 60) * 60;    // boundary just crossed
            label = fmtMMSS(markSec);
            offset = curOffset + (remSec - markSec);         // ≤ curOffset
        } else {
            // Pre/post session: the wall-clock whole minute just crossed.
            mode = 'CLOCK';
            const floorMs = Math.floor(messageBus.clockTime.getTime() / 60000) * 60000;
            label = fmtHHMM(floorMs + (state.gmtOffset || 0));
            offset = (floorMs - startMs) / 1000;
        }

        if (modeEl) modeEl.textContent = mode;
        // Enabled when the marker is a valid offset. Normally it must be at/before
        // the playhead (a past boundary); the Lap 1 marker may be AHEAD (forward
        // sync — the seek clamps to the data edge server-side).
        const maxOffset = allowForward ? Infinity : curOffset + 0.5;
        const ok = label != null && offset != null && offset >= 0 && offset <= maxOffset;
        btn.textContent = label != null ? label : '--';
        btn.disabled = !ok;
        btn._syncOffset = ok ? offset : null;
    }

    function updateClocks() {
        // The track time and the session-time countdown are both derived
        // from the playback clock (messageBus) so they respond correctly
        // to pause, play and skip — freezing when paused, jumping on seek.

        // Local (track) time. state.utc is only a fallback for the very
        // first frame before any clock update has arrived.
        const trackTimeEl = document.getElementById('trackTime');
        if (trackTimeEl && state.gmtOffset !== null) {
            let localMs;
            if (messageBus.clockTime) {
                localMs = messageBus.clockTime.getTime() + state.gmtOffset;
            } else if (state.utc) {
                localMs = state.utc.getTime() + state.gmtOffset;
            }
            if (localMs !== undefined) {
                trackTimeEl.textContent = new Date(localMs).toUTCString().slice(17, 25);
            }
        }

        // Session-time countdown. Only extrapolate while F1's clock is
        // actually running (clockStatus === 'play', i.e. ExtrapolatedClock
        // .Extrapolating === true). Pre-session, red flag, and any other
        // F1-paused state freezes the display at the most recent anchor
        // instead of ticking forward — fixes the historical "session
        // clock jumps on pause/play" symptom.
        const sessionClockEl = document.getElementById('sessionClock');
        if (sessionClockEl && state.sessionTimeMs !== null && state.firstNonZeroSeen) {
            let remaining = state.sessionTimeMs;
            if (state.clockStatus === 'play'
                    && state.sessionTimeAnchorMs !== null
                    && messageBus.clockTime) {
                const currentOffsetMs = messageBus.getCurrentOffset() * 1000;
                remaining = state.sessionTimeMs - (currentOffsetMs - state.sessionTimeAnchorMs);
            }
            remaining = Math.max(0, remaining);
            sessionClockEl.textContent = formatSessionTime(remaining, state.sessionTimeFormat);
        }

        // (Audio scrubber + audio-time readout removed — the offset
        // input alone tells the user the data-vs-audio relationship,
        // and the traffic light tells them whether audio is playing.)

        // Traffic light (green/yellow/red) — updated each frame so it
        // reflects buffering / seeking transitions promptly.
        updateAudioStatusLight();

        updateSyncButtons();

        state.clockAnimId = requestAnimationFrame(updateClocks);
    }

    // (adjustAudio / audioScrubberSeek removed — Audio Delay input is
    // now the only way to manually shift audio relative to the data
    // clock; data-scrubber seek still pulls audio along automatically.)

    // How long of the audio is playable right now. Prefer the audio
    // element's own duration; otherwise use the session duration so
    // the scrubber maps the audible timeline to roughly the same span
    // as the data scrubber. (Date.now() − startUtc was wrong for past
    // replays — gave days-long denominators and a stationary dot.)
    function formatSessionTime(ms, format) {
        const totalSec = Math.floor(ms / 1000);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;

        if (format === 'hms') {
            return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        }
        // 'ms' format
        const totalMin = h * 60 + m;
        return `${totalMin}:${String(s).padStart(2, '0')}`;
    }

    // =========================================================================
    // Track Status
    // =========================================================================

    // Server-computed badge state {status, message}. The client only maps
    // the status enum to a colour class and renders the message text.
    const TRACK_STATUS_COLOR = {
        green: 'green',
        red: 'red',
        sc: 'yellow',
        vsc: 'yellow',
        inactive: 'white',
        finished: 'white',
    };

    // Suppress the change-blink during a restore/seek re-emit (not a live
    // transition), same pattern as _radioRestoring below. (HBAKIcye)
    let _tsRestoring = false;
    function handleTrackStatus(data) {
        if (!data || typeof data !== 'object') return;

        const color = TRACK_STATUS_COLOR[data.status] || 'white';
        const text = data.message || '--';
        const changed = (text !== state.trackStatusText || color !== state.trackStatusColor);

        state.trackStatusText = text;
        state.trackStatusColor = color;

        const el = document.getElementById('trackStatus');
        const textEl = document.getElementById('trackStatusText');
        if (el) el.className = `track-status ${color}`;   // resets classes (clears ts-blink)
        if (textEl) textEl.textContent = text;
        // Blink twice on a genuine status change to draw the eye. Skip the
        // restore/seek re-emit so a replay seek doesn't flash. (HBAKIcye)
        if (el && changed && !_tsRestoring) {
            void el.offsetWidth;            // reflow so re-adding restarts the animation
            el.classList.add('ts-blink');
        }
    }
    messageBus.on('state:reset', () => { _tsRestoring = true; });
    messageBus.on('state:seek-complete', () => { _tsRestoring = false; });

    // =========================================================================
    // Playback Controls
    // =========================================================================

    function handlePlaybackStatus(data) {
        const playBtn = document.getElementById('playPauseBtn');
        const playIcon = document.getElementById('playIcon');
        if (playIcon) {
            playIcon.innerHTML = data.isPlaying ? '&#10074;&#10074;' : '&#9658;';
        }
        if (playBtn) {
            playBtn.classList.toggle('playing', data.isPlaying);
        }

        // Track + session time are derived from the playback clock in
        // updateClocks, so play/pause needs no re-anchoring here.
        startClockAnimation();
    }

    function handleClockUpdate(data) {
        const prevDuration = state.duration;
        state.offset = data.offset || 0;
        state.duration = data.duration || state.duration;
        updateScrubberPosition();
        updateGoLiveButton();
        // Re-render event markers when the scrubber's coordinate system moves
        // under them:
        //  - LIVE: the edge grows every tick (and the no-spoiler rule reveals
        //    events progressively as playback reaches them).
        //  - REPLAY still building: duration follows the growing build edge, so
        //    a fixed event offset maps to a new x — the markers must be
        //    re-projected (card 79). A finished replay has a fixed duration, so
        //    this is a no-op once the build completes.
        // Re-render markers when the coordinate system moved (duration grew — live edge or
        // replay build) OR a hidden event just crossed the playhead (no-spoiler reveal).
        const visN = (state.events || []).reduce(
            (n, ev) => n + (ev.offset_ms <= state.offset * 1000 ? 1 : 0), 0);
        if (state.events && (state.duration !== prevDuration || visN !== state._lastVisN)) {
            state._lastVisN = visN;
            renderEventMarkers(state.events);
        }
        startClockAnimation();
    }

    // In LIVE the speed button is replaced by the LIVE button; in REPLAY
    // the LIVE button is hidden and the speed button is shown. isLive is
    // server-authoritative (from state:full). The LIVE button is red when
    // streaming the most recent data (within 3 s of the live edge) and
    // black when lagging behind it; clicking it (seekLive) snaps to live.
    function updateGoLiveButton() {
        const btn = document.getElementById('goLiveBtn');
        const speedBtn = document.getElementById('speedBtn');
        const isLive = !!messageBus.isLive;
        if (speedBtn) speedBtn.classList.toggle('hidden', isLive);
        if (!btn) return;
        if (!isLive) {
            btn.classList.add('hidden');
            return;
        }
        btn.classList.remove('hidden');
        // Caught-up = within a tolerance of the live edge. Drop the lower
        // bound: clock jitter can push offset a hair past duration (lag
        // slightly negative) while genuinely AT live — keeping `lag >= 0`
        // made the indicator blink red↔black. Anything from "at/ahead of the
        // edge" through "≤3 s behind" counts as live (I9).
        const lag = state.duration - state.offset;
        btn.classList.toggle('at-live', lag <= 3);
    }

    // Speed button. Replay only. 30x/60x were dropped — too fast to be useful
    // (they create more problems than they solve).
    const SPEEDS = [1, 2, 5, 10];
    const SPEED_KEY = 'f1.playbackSpeed';

    // Apply a playback speed and persist it. The UI button only cycles SPEEDS (1x-10x), but the
    // server accepts 0.1x-10x: sub-1x is a dev-only slow-motion band for smooth screen-capture,
    // reachable from the console via window.F1SetSpeed(0.1) and remembered in localStorage so it
    // survives reloads/reconnects. Not exposed in the UI. (card TDaS5wwz)
    function setPlaybackSpeed(v) {
        v = Number(v);
        if (!isFinite(v)) return;
        v = Math.max(0.1, Math.min(v, 10));           // mirror the server clamp
        messageBus.send({ cmd: 'speed', value: v });
        messageBus.speed = v;
        try { localStorage.setItem(SPEED_KEY, String(v)); } catch (e) {}
        const btn = document.getElementById('speedBtn');
        if (btn) btn.textContent = `${v}x`;
    }
    window.F1SetSpeed = setPlaybackSpeed;             // console entry point for slow motion

    function cycleSpeed() {
        const idx = SPEEDS.indexOf(messageBus.speed || 1);   // -1 (e.g. from slow-mo) → back to 1x
        setPlaybackSpeed(SPEEDS[(idx + 1) % SPEEDS.length]);
    }

    // Re-apply a stored slow-motion speed once per connect (the server resets to 1x on connect).
    let _speedRestored = false;
    messageBus.on('playback:status', () => {
        if (_speedRestored) return;
        _speedRestored = true;
        let stored = NaN;
        try { stored = parseFloat(localStorage.getItem(SPEED_KEY)); } catch (e) {}
        if (isFinite(stored) && stored !== (messageBus.speed || 1)) setPlaybackSpeed(stored);
    });

    // =========================================================================
    // Scrubber
    // =========================================================================

    // Scrubber is piecewise-linear over up to 3 regions so the boring lead-in
    // and post-session tail are each compressed to ~5px, leaving the bulk of
    // the width for the actual session. Boundaries (session-offset ms):
    //   region 1: [0, T1-5min]              -> [0px, 5px]
    //   region 2: [T1-5min, T2+5min]        -> [5px, X-5px]
    //   region 3: [T2+5min, end (= Tl/Tf)]  -> [X-5px, X]
    // T1 = first playback event, T2 = last (chequered / session end). In LIVE,
    // `end` is the live edge and grows; region 3 only appears once the edge
    // passes T2+5min (until then region 2 runs to the edge). With no usable
    // events it falls back to a linear [0, end] mapping. Built as sorted
    // [timeMs, pct] control points; offsetToPct / pctToOffset interpolate.
    const SIDE_PX = 5;
    const FIVE_MIN_MS = 5 * 60 * 1000;

    function scrubberCtrl() {
        const end = state.duration * 1000;
        if (end <= 0) return [[0, 0], [1, 100]];
        const el = document.getElementById('scrubber');
        const width = el && el.clientWidth > 0 ? el.clientWidth : 800;
        const sidePct = Math.min(45, (SIDE_PX / width) * 100);

        // Scrubber events are all `event` track-status markers now (GREEN /
        // RED / SC / VSC / CHEQUERED). T1 = first such marker (≈ session/race
        // green), T2 = chequered (or the last marker). The middle section spans
        // T1→T2; pre-T1 and post-T2 are compressed into the side margins.
        let chequered = null, firstVisible = null;
        for (const ev of state.events || []) {
            if (typeof ev.offset_ms !== 'number') continue;
            const d = typeof ev.data === 'string' ? ev.data : (ev.data?.event || '');
            const upper = String(d).toUpperCase();
            if (upper === 'CHEQUERED')
                chequered = (chequered === null) ? ev.offset_ms : Math.max(chequered, ev.offset_ms);
            if (firstVisible === null || ev.offset_ms < firstVisible) firstVisible = ev.offset_ms;
        }
        const t1 = firstVisible;
        if (t1 == null) return [[0, 0], [end, 100]];

        // Region 2 ends at the CHEQUERED flag (real session end) — only a finished session
        // has a post-session tail to compress into region 3. No-spoiler: use the flag only
        // once the playhead has actually REACHED it; until then (still building, live
        // before the end, or simply not there yet) region 2 runs to the growing edge, so
        // the scrubber keeps resizing and the end position isn't revealed early. (Previously
        // the last discovered event capped region 2, freezing a transient replay's scrubber
        // between events — that rule was only valid for fully-built DBs.) (user 2026-07-08)
        const t2 = (chequered != null && chequered <= state.offset * 1000) ? chequered : null;
        const Y = Math.max(0, Math.min(t1 - FIVE_MIN_MS, end));
        const pts = [[0, 0]];
        if (Y > 0) pts.push([Y, sidePct]);
        if (t2 != null && t2 > t1) {
            const Zraw = t2 + FIVE_MIN_MS;
            const Z = Math.min(Zraw, end);
            const rightPct = (Zraw < end - 1) ? (100 - sidePct) : 100;
            if (Z > Y) pts.push([Z, rightPct]);
        }
        if (pts[pts.length - 1][0] < end) pts.push([end, 100]);
        return pts;
    }

    // Monotonic piecewise interpolation over the control points. xi/yi select
    // the input/output column (0 = timeMs, 1 = pct).
    function _scrubInterp(pts, x, xi, yi) {
        const lo = pts[0], hi = pts[pts.length - 1];
        if (x <= lo[xi]) return lo[yi];
        if (x >= hi[xi]) return hi[yi];
        for (let i = 1; i < pts.length; i++) {
            if (x <= pts[i][xi]) {
                const a = pts[i - 1], b = pts[i];
                const span = b[xi] - a[xi];
                if (span <= 0) return b[yi];
                return a[yi] + ((x - a[xi]) / span) * (b[yi] - a[yi]);
            }
        }
        return hi[yi];
    }

    function offsetToPct(offset_ms) {
        return Math.max(0, Math.min(100, _scrubInterp(scrubberCtrl(), offset_ms, 0, 1)));
    }

    function pctToOffset(pct) {
        return Math.max(0, _scrubInterp(scrubberCtrl(), pct, 1, 0));
    }

    function updateScrubberPosition() {
        const dot = document.getElementById('scrubberDot');
        if (!dot || state.duration <= 0) return;
        const cur_ms = state.offset * 1000;
        const pct = Math.max(0, Math.min(100, offsetToPct(cur_ms)));
        dot.style.left = `${pct}%`;
    }

    function initScrubber() {
        const scrubber = document.getElementById('scrubber');
        if (!scrubber) return;

        scrubber.addEventListener('click', (e) => {
            // If the click landed on a flag/event marker, jump to its
            // pre-computed seek target (event offset − 60 s).
            const flag = e.target.closest('[data-seek-offset]');
            if (flag) {
                e.stopPropagation();
                const target = parseInt(flag.dataset.seekOffset, 10);
                if (!isNaN(target)) {
                    seekToOffset(target / 1000);
                }
                return;
            }
            const rect = scrubber.getBoundingClientRect();
            const pct = ((e.clientX - rect.left) / rect.width) * 100;
            const offset_ms = Math.max(0, pctToOffset(pct));
            seekToOffset(offset_ms / 1000);
        });

        // Tooltip on hover
        scrubber.addEventListener('mousemove', (e) => {
            const tooltip = document.getElementById('scrubberTooltip');
            if (!tooltip || state.duration <= 0) return;
            const rect = scrubber.getBoundingClientRect();
            const pct = ((e.clientX - rect.left) / rect.width) * 100;
            const offset = Math.max(0, pctToOffset(pct) / 1000);
            const h = Math.floor(offset / 3600);
            const m = Math.floor((offset % 3600) / 60);
            const s = Math.floor(offset % 60);
            tooltip.textContent = `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            tooltip.style.left = `${e.clientX - rect.left}px`;
            tooltip.style.display = 'block';
        });

        scrubber.addEventListener('mouseleave', () => {
            const tooltip = document.getElementById('scrubberTooltip');
            if (tooltip) tooltip.style.display = 'none';
        });

        // Re-render on resize so the side-segment widths track the
        // scrubber's actual pixel width.
        window.addEventListener('resize', () => {
            updateScrubberPosition();
            if (state.events) renderEventMarkers(state.events);
        });
    }

    function renderEventMarkers(events) {
        const container = document.getElementById('scrubberEvents');
        if (!container || state.duration <= 0) return;

        let html = '';

        // Each event position uses the non-linear scrubber mapping so
        // the visible scrubber width is dominated by the 5-min-before-
        // start → chequered window. Overlapping flags are allowed (=
        // user-spec'd: 0.5 px dark stroke on each SVG handles the
        // visual separation).
        for (const ev of events) {
            const pct = offsetToPct(ev.offset_ms);
            if (pct < 0 || pct > 100) continue;
            // No-spoiler rule (live + replay): ALWAYS inject every marker (so the scrubber's
            // full extent + limits are stable), but hide the ones ahead of the playhead with a
            // CSS class. They reveal progressively as the playhead crosses them (handleClockUpdate
            // re-renders on that). (user 2026-07-13)
            const sp = ev.offset_ms > state.offset * 1000 ? ' evt-spoiler' : '';

            const d = typeof ev.data === 'string' ? ev.data : (ev.data?.event || ev.data || '');
            const upper = String(d).toUpperCase();

            // data-offset = the SEEK target (event offset − 60 s, clamped
            // to ≥ 0) — 60 s lead-in gives the audio time to seek and
            // the user a chance to sync visually before the event.
            const seekOffset = Math.max(0, ev.offset_ms - 60000);
            const dataAttrs = `data-seek-offset="${seekOffset}"`;

            let marker = '';
            let title = `${d} — click to skip to 60 s before`;

            // Pole-less waving-flag SVG — 16 × 16 footprint (= same as
            // chequered emoji) with wavy top + wavy bottom. 0.5 px
            // dark stroke gives visual separation when flags overlap.
            // Same shape re-used for track-limits + blue-flag indicators
            // in the standings tile.
            const flagSvg = `<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" stroke="rgba(0,0,0,0.7)" stroke-width="0.5">`
                          + `<path d="M1 3 Q4 1 8 3 T15 3 V13 Q12 15 8 13 T1 13 Z"/>`
                          + `</svg>`;
            if (upper === 'CHEQUERED') {
                marker = `<div class="scrubber-flag chequered${sp}" style="left:${pct}%" title="${title}" ${dataAttrs}>&#127937;</div>`;
            } else if (upper === 'RED' || upper === 'RED FLAG') {
                marker = `<div class="scrubber-flag red${sp}" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else if (upper.includes('SC') || upper.includes('VSC') || upper.includes('SAFETY')) {
                marker = `<div class="scrubber-flag yellow${sp}" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else if (upper === 'GREEN') {
                marker = `<div class="scrubber-flag green${sp}" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else {
                marker = `<div class="scrubber-event${sp}" style="left:${pct}%;background:#888" title="${title}" ${dataAttrs}></div>`;
            }

            html += marker;
        }

        // (The old scrubber "LIVE" marker is gone — the header LIVE button,
        // which replaces the speed button in live, is the single live control.)
        container.innerHTML = html;
    }

    // Live mode: periodically update duration from latest message offset
    state.liveInterval = null;
    function startLiveTracking() {
        if (state.liveInterval) return;
        state.liveInterval = setInterval(() => {
            // Stop tracking once the chequered flag has flown (the session's
            // final marker now that playbackEvent/sessionEnd is gone). The
            // server caps duration at chequered + 5 min, so the scrubber's
            // tail is governed server-side regardless.
            const hasEnd = state.events.some(e => {
                const d = typeof e.data === 'string' ? e.data : (e.data?.event || '');
                return String(d).toUpperCase() === 'CHEQUERED';
            });
            if (hasEnd) {
                clearInterval(state.liveInterval);
                state.liveInterval = null;
                renderEventMarkers(state.events);
                return;
            }
            // Update duration from max offset across all received messages
            if (state.maxReceivedOffset > state.duration) {
                state.duration = state.maxReceivedOffset;
                renderEventMarkers(state.events);
            }
        }, 3000);
    }
    state.maxReceivedOffset = 0;

    // =========================================================================
    // Audio Commentary
    // =========================================================================

    function initAudio(audioInfo) {
        const controls = document.getElementById('audioControls');

        if (!audioInfo || !audioInfo.file) {
            if (controls) controls.classList.add('disabled');
            return;
        }

        const sessionName = new URLSearchParams(window.location.search).get('session');
        if (!sessionName) {
            if (controls) controls.classList.add('disabled');
            return;
        }

        // Reconnect (server/capture restart): the audio element and its byte-level
        // fetch survive — the concat file extends seamlessly and the MSE poll loop
        // retries through the outage. Only the SEGMENT MAP changes, so refresh it
        // in place (a new segment lands at its own PDT anchor) and keep playing.
        // Preserve the user's manual delay (offsetSeconds) + the live muxer length.
        // Single vs multi is the same code path — the map is authoritative. (hwu52Zqy)
        if (state.audio.element) {
            if (audioInfo.start_utc) {
                state.audio.startUtc = new Date(audioInfo.start_utc.replace('Z', '+00:00'));
            }
            state.audio.segments = (audioInfo.segments || [])
                .filter(s => s.start_utc && s.duration > 0)
                .map(s => ({ startMs: new Date(s.start_utc.replace('Z', '+00:00')).getTime(),
                             duration: s.duration }));
            return;
        }

        // Single endpoint — server decides whether to tail-follow (capture
        // in progress) or serve the static file with range support.
        const audioUrl = `/api/v1/livetiming/audio/${encodeURIComponent(sessionName)}`;
        const audio = new Audio();
        audio.preload = 'auto';
        audio.loop = false;
        state.audio.element = audio;
        // Expose the element so other modules (= tv_sync.js) can read
        // its actual playback position without depending on this IIFE's
        // private state.
        window.f1audioElement = audio;
        // Audio telemetry probes (opt-in; no-op unless enabled). `waiting`/
        // `stalled` are the underrun (the pause itself); `playing` is recovery.
        ['play', 'playing', 'pause', 'waiting', 'stalled', 'seeking', 'seeked',
         'ended', 'error', 'ratechange', 'suspend'].forEach(function (evt) {
            audio.addEventListener(evt, function () { atel('el:' + evt, bufSnap(audio)); });
        });
        // Manual audio delay. The PDT broadcast anchor is sometimes offset (see
        // README known issues), so let the user shift the audio against the data
        // clock. `offsetSeconds` is `off` in clockToAudioSec: positive → audio
        // plays LATER (hear older content), negative → audio plays EARLIER.
        function setAudioOffset(sec) {
            if (!state.audio.isReady || !isFinite(sec)) { updateAudioDelayInput(); return; }
            state.audio.offsetSeconds = sec;
            updateAudioDelayInput();
            // Audio-only re-seek: the delay folds into clockToAudioSec, so
            // re-placing the audio at the current data clock applies it. (B05)
            if (messageBus.clockTime) placeAudioAtClock(messageBus.clockTime.getTime(), true);
            syncAudio();
        }
        // Absolute (Delay input box, ss.SSS) and relative (tv_sync.js) entry points.
        window.applyAudioDelay = function(raw) { setAudioOffset(parseFloat(raw)); };
        window.adjustAudioOffset = function(deltaSec) { setAudioOffset((state.audio.offsetSeconds || 0) + deltaSec); };

        if (audioInfo.start_utc) {
            state.audio.startUtc = new Date(audioInfo.start_utc.replace('Z', '+00:00'));
        }
        state.audio.offsetSeconds = audioInfo.offset_seconds || 0;
        updateAudioDelayInput();
        // Per-segment map for piecewise clock→audio mapping + inter-segment gap
        // skipping on multi-segment replays (I15). Empty → single-anchor path.
        state.audio.segments = (audioInfo.segments || [])
            .filter(s => s.start_utc && s.duration > 0)
            .map(s => ({ startMs: new Date(s.start_utc.replace('Z', '+00:00')).getTime(),
                         duration: s.duration }));
        // True current audio length (s) — set by the MSE muxer as it indexes the
        // (possibly still-growing) file; lets clockToAudioSec extend a live last
        // segment past its connect-time snapshot duration. null → non-MSE path.
        state.audio.totalAudioSec = null;

        audio.volume = state.audio.isMuted ? 0 : state.audio.volume;
        state.audio.isReady = true;

        // Audio timestamp is updated every frame from updateClocks() so
        // it stays in lockstep with the session clock; no need for the
        // (slower, ~250 ms) timeupdate event.

        if (controls) controls.classList.remove('disabled');

        updateMuteButton();
        // Initial slider position from persisted state.audio.volume.
        const slider = document.getElementById('volumeSlider');
        if (slider) slider.value = String(Math.round(state.audio.volume * 100));

        // Source. Preferred: MSE — range-fetch the .aac, transmux ADTS→fMP4
        // in-browser, feed a SourceBuffer (Route A). This makes LIVE audio
        // natively seekable EXACTLY like replay (no chunked stream, no ?t=
        // byte estimate). Fallback (no MSE/codec): the legacy stream URL, which
        // the server still serves chunked (live) / range (replay).
        state.audio.mse = mseAudioSupported();
        state.audio.mseSeek = null;
        if (state.audio.mse) startMseAudio(audio, audioUrl);
        else audio.src = audioUrl;

        // Once we have an initial clock position, place the audio at the data
        // clock so they start together (native seek on the seekable buffer).
        const _alignOnce = () => { if (messageBus.clockTime) placeAudioAtClock(messageBus.clockTime.getTime(), true); };
        audio.addEventListener('loadedmetadata', _alignOnce, { once: true });
        // Also try once we have a confirmed clock; loadedmetadata may
        // not fire reliably on chunked transfer.
        setTimeout(_alignOnce, 500);
    }

    // ── Audio telemetry probe helpers (opt-in; no-op unless enabled) ─────
    // Record an audio-pipeline event with data-clock context, so an audio stall
    // can be correlated with the data-stream (B07) + the speaker recording.
    function atel(type, fields) {
        const t = window.f1audioTelemetry;
        if (!t || !t.isEnabled()) return;
        const f = fields || {};
        try {
            if (typeof messageBus !== 'undefined') {
                if (messageBus.clockTime) f.clockMs = messageBus.clockTime.getTime();
                if (typeof messageBus.getCurrentOffset === 'function') f.offset = messageBus.getCurrentOffset();
            }
        } catch (e) { /* context is best-effort */ }
        t.record(type, f);
    }
    // Buffered-headroom snapshot: seconds of audio ahead of the playhead (the
    // margin before an underrun) plus element readyState/paused.
    function bufSnap(a) {
        const o = { curr: null, bufEnd: null, headroom: null,
                    ready: a && a.readyState, paused: a && a.paused };
        try {
            o.curr = a.currentTime;
            const b = a.buffered;
            if (b && b.length) { o.bufEnd = b.end(b.length - 1); o.headroom = o.bufEnd - a.currentTime; }
        } catch (e) { /* buffered can throw */ }
        return o;
    }

    // ── MSE audio (Route A: client transmux) ─────────────────────────────
    const MSE_MIME = 'audio/mp4; codecs="mp4a.40.2"';
    function mseAudioSupported() {
        try {
            return typeof MediaSource !== 'undefined'
                && MediaSource.isTypeSupported(MSE_MIME)
                && typeof AacFmp4 !== 'undefined' && !!AacFmp4;
        } catch (e) { return false; }
    }

    // Range-fetch the .aac, transmux ADTS→fMP4 in-browser, feed an MSE
    // SourceBuffer — live and replay alike (one uniform loop). The muxer keeps an
    // EXACT frame index (AAC-LC = fixed 1024 samples/frame), so any seek lands on
    // the exact frame. The SourceBuffer is a WINDOW around the play head (Firefox
    // caps audio at ~12 min); a reconciling state machine clears/evicts/extends it
    // toward [wantFrom, wantTo). The fetch only builds the index (raw + offsets);
    // segments are cut from the in-memory bytes on demand — no re-fetch on seek.
    function startMseAudio(audio, audioUrl) {
        const url = audioUrl + (audioUrl.indexOf('?') >= 0 ? '&' : '?') + 'seekable=1';
        const ms = new MediaSource();
        audio.src = URL.createObjectURL(ms);
        ms.addEventListener('sourceopen', function onOpen() {
            ms.removeEventListener('sourceopen', onOpen);
            let sb;
            try { sb = ms.addSourceBuffer(MSE_MIME); }
            catch (e) { adbg('MSE addSourceBuffer failed — fallback to stream URL', e); state.audio.mse = false; state.audio.mseSeek = null; audio.src = audioUrl; return; }
            const mux = AacFmp4.create();
            const FETCH = 4 * 1024 * 1024;      // fetch chunk size (bytes)
            const AHEAD_S = 300, BEHIND_S = 45; // SourceBuffer window around the play head (s); < Firefox ~12-min cap
            const SEG_FRAMES = 1024;            // frames per appended segment (~22 s)
            let fetched = 0, pollTimer = null, initDone = false;
            let haveFrom = 0, haveTo = 0;       // appended frame range [haveFrom, haveTo) (empty when equal)
            let wantFrom = 0, wantTo = 0, rebuildAt = 0, clearing = false;

            function framesPer(sec) { return Math.round(sec * mux.sampleRate() / 1024); }

            // Reconcile the SourceBuffer toward [wantFrom, wantTo): ONE async op per
            // call (clear → init → evict-behind → extend-forward), repeated on each
            // updateend until settled.
            function reconcile() {
                if (!sb || sb.updating || !mux.ready()) return;
                if (clearing) {
                    if (sb.buffered.length) { try { sb.remove(sb.buffered.start(0), sb.buffered.end(sb.buffered.length - 1) + 1); } catch (_) {} return; }
                    clearing = false; haveFrom = haveTo = rebuildAt;          // cleared → window starts at the target frame
                }
                if (!initDone) { try { sb.appendBuffer(mux.init()); initDone = true; } catch (e) { adbg('MSE init append err', e); } return; }
                if (wantFrom > haveFrom && sb.buffered.length) {              // evict behind
                    try { sb.remove(0, mux.frameTime(wantFrom)); haveFrom = wantFrom; } catch (_) {}
                    return;
                }
                const target = Math.min(wantTo, mux.frameCount());           // only append fetched frames
                if (target > haveTo) {                                       // extend forward (one segment, exact time)
                    const i1 = Math.min(haveTo + SEG_FRAMES, target);
                    try { sb.appendBuffer(mux.segment(haveTo, i1)); haveTo = i1; }
                    catch (e) { if (!(e && e.name === 'QuotaExceededError')) adbg('MSE seg append err', e); }
                }
            }
            sb.addEventListener('updateend', reconcile);
            sb.addEventListener('error', () => adbg('MSE SourceBuffer "error" event'));
            audio.addEventListener('error', () => adbg('MSE audio element error, code', audio.error && audio.error.code));
            adbg('MSE SourceBuffer created for', MSE_MIME);

            // Aim the window at `centerSec`. Play head outside the buffered range →
            // clear + rebuild starting at the EXACT target frame; else evict behind
            // + extend forward. Idempotent — safe to call often (fetch / timeupdate / seek).
            function setWindow(centerSec) {
                if (!mux.ready()) return;
                const i = mux.frameAt(centerSec);
                const lo = Math.max(0, i - framesPer(BEHIND_S)), hi = i + framesPer(AHEAD_S);
                const empty = (haveFrom === haveTo);
                if (!empty && (i < haveFrom || i > haveTo)) {                // disjoint → rebuild at the target frame
                    clearing = true; rebuildAt = i;
                } else if (empty) {
                    haveFrom = haveTo = i;
                }
                wantFrom = lo; wantTo = hi;
                reconcile();
            }

            // One uniform loop for live AND replay: pull bytes from the fetch
            // offset, index them (mux.feed), catch up fast, then idle-poll for
            // growth (a live file grows; a replay returns 416 — same code).
            async function poll() {
                let again = 3000;
                const t0 = performance.now();
                try {
                    const resp = await fetch(url, { headers: { Range: 'bytes=' + fetched + '-' + (fetched + FETCH - 1) } });
                    if (resp.status === 206 || resp.status === 200) {
                        const buf = new Uint8Array(await resp.arrayBuffer());
                        if (buf.length) { mux.feed(buf); fetched += buf.length; }
                        if (mux.ready()) state.audio.totalAudioSec = mux.duration();  // true length → clockToAudioSec
                        if (buf.length >= FETCH) again = 0;
                        setWindow(audio.currentTime);                        // extend with newly-indexed frames
                        atel('fetch', { status: resp.status, bytes: buf.length, fetched: fetched, ms: Math.round(performance.now() - t0) });
                    } else {
                        // 416 at the live edge = no new audio yet (client starved) — worth recording.
                        atel('fetch', { status: resp.status, bytes: 0, fetched: fetched, ms: Math.round(performance.now() - t0) });
                    }
                } catch (e) { adbg('MSE fetch error', e); atel('fetch:error', { fetched: fetched, err: String((e && e.message) || e) }); }
                pollTimer = setTimeout(poll, again);
            }

            // Re-window on a seek (lands on the exact frame); also extend during playback.
            state.audio.mseSeek = function (targetSec) { setWindow(targetSec); };
            let _telTick = 0;
            audio.addEventListener('timeupdate', function () {
                setWindow(audio.currentTime);
                const now = performance.now();
                if (now - _telTick >= 1000) { _telTick = now; atel('tick', bufSnap(audio)); }   // ~1/s headroom trace
            });

            poll();
        });
    }

    // Debug: enable with  localStorage.setItem('audioSyncDebug','1')  then reload.
    function adbg(...a) { try { if (localStorage.getItem('audioSyncDebug') === '1') console.log('[audio-sync]', ...a); } catch (e) {} }

    // Map a data-clock instant (ms) → position in the combined audio stream
    // (seconds), using the per-segment map so inter-segment capture gaps are
    // skipped (I15). Returns null when the clock is in a gap or before the
    // first segment → caller pauses until the next segment's content begins.
    function clockToAudioSec(clockMs) {
        const off = state.audio.offsetSeconds || 0;
        const segs = state.audio.segments;
        if (!segs || !segs.length) {                       // single-anchor fallback
            if (!state.audio.startUtc) return null;
            return (clockMs - state.audio.startUtc.getTime()) / 1000 - off;
        }
        let cum = 0;
        for (let k = 0; k < segs.length; k++) {
            const s = segs[k];
            if (clockMs < s.startMs) return null;          // before this segment → gap
            // The LAST segment may still be growing (live capture): its true end
            // is the audio that actually exists now (the muxer's exact duration),
            // not the connect-time snapshot. For a completed replay file the muxer
            // duration equals the summed segment durations, so this is a no-op.
            let dur = s.duration;
            if (k === segs.length - 1 && state.audio.totalAudioSec != null) {
                dur = Math.max(dur, state.audio.totalAudioSec - cum);
            }
            if (clockMs < s.startMs + dur * 1000) {
                return cum + (clockMs - s.startMs) / 1000 - off;
            }
            cum += dur;
        }
        return null;                                       // past the last segment → no audio here
    }

    // Diagnostic: is time `t` inside any buffered range of the element?
    function targetWithinBuffered(audio, t) {
        for (let i = 0; i < audio.buffered.length; i++) {
            if (t >= audio.buffered.start(i) - 0.1 && t <= audio.buffered.end(i) + 0.1) return true;
        }
        return false;
    }

    // THE audio-seek primitive (B05). Positions the <audio> element so the
    // audible content matches the data-clock instant `clockMs` — the manual
    // delay folds in via clockToAudioSec's offsetSeconds, so "seek audio to Y"
    // is exactly this call. Everything routes through here: init, explicit
    // seek (hard=true, place exactly), and the continuous playback drift-nudge
    // (hard=false, only correct sustained drift > AUDIO_NUDGE_S so a steady
    // clock:update stream doesn't thrash the element). Returns:
    //   'ok'      — positioned (or already within tolerance)
    //   'gap'     — no audio content at this instant (pre-audio / inter-segment
    //               gap / past end) → element paused
    //   'unready' — element/clock/source not ready yet
    function placeAudioAtClock(clockMs, hard) {
        const audio = state.audio.element;
        if (!audio || !state.audio.startUtc) return 'unready';
        let targetSec = clockToAudioSec(clockMs);
        // In an inter-segment gap / before the audio window / past the end
        // (I15): no audio for this data-time → pause.
        if (targetSec === null) { if (!audio.paused) audio.pause(); return 'gap'; }
        // Clamp to 0 — a delay/nudge that pushes the target before byte 0
        // should land at the start, not fall silent.
        if (targetSec < 0) targetSec = 0;
        const seekable = audio.seekable && audio.seekable.length > 0
            && audio.seekable.end(audio.seekable.length - 1) > 0;
        // Past the true end of a completed replay file → pause (segment map
        // already returns null past the last segment, so this only guards the
        // single-anchor fallback).
        if (seekable && isFinite(audio.duration) && targetSec > audio.duration) {
            if (!audio.paused) audio.pause();
            return 'gap';
        }
        // MSE SourceBuffer holds only a ~12-min window; re-window around a far
        // target before seeking into it.
        if (state.audio.mse && state.audio.mseSeek && !targetWithinBuffered(audio, targetSec)) {
            adbg('place: target unbuffered → MSE re-window', targetSec.toFixed(1));
            state.audio.mseSeek(targetSec);
        }
        // Non-seekable fallback (no-MSE live tail-follow): can't reposition —
        // let it play through. MSE is the supported seek path (client_simple).
        if (!seekable) return audio.readyState < 1 ? 'unready' : 'ok';
        const threshold = hard ? 0.1 : AUDIO_NUDGE_S;
        if (Math.abs(audio.currentTime - targetSec) > threshold) {
            adbg('place:', hard ? 'SEEK' : 'nudge', audio.currentTime.toFixed(2), '→', targetSec.toFixed(2));
            audio.currentTime = targetSec;
        }
        return 'ok';
    }
    // The seek primitive (base.js) drives audio through this hook.
    window.f1placeAudio = placeAudioAtClock;

    // For the audio status traffic light: green when audio's playback
    // position is within ±5 s of the data clock, red otherwise.
    function audioSyncDrift() {
        const audio = state.audio.element;
        if (!audio || !state.audio.startUtc || !messageBus.clockTime) return null;
        const targetSec = clockToAudioSec(messageBus.clockTime.getTime());
        if (targetSec === null) return null;
        return audio.currentTime - targetSec;
    }
    window.f1audioSyncDrift = audioSyncDrift;

    // Status footer (card 4N7VgVlf): is there audio CONTENT at the current
    // playhead? clockToAudioSec returns null in inter-segment gaps and outside
    // the capture window, so the footer can show the real bitrate vs 0.
    window.f1audioAvailableNow = function () {
        if (!state.audio.element || !state.audio.isReady || !state.audio.startUtc
                || !messageBus.clockTime) return false;
        return clockToAudioSec(messageBus.clockTime.getTime()) !== null;
    };


    // Continuous-playback drift tolerance (s). placeAudioAtClock(hard=false)
    // only re-seeks when |audio − data clock| exceeds this, so the 60fps
    // clock:update stream can't thrash the element. This is the light nudge
    // that replaced the old settle-guard + ?t= drift-reload loop. (B05)
    const AUDIO_NUDGE_S = 0.75;

    // Audio play-state model — independent of the data play state.
    //   'sync'    — audio follows data: plays iff data is playing
    //               (default; reset by Space).
    //   'playing' — audio plays regardless of data state (set by P
    //               while audio was paused).
    //   'paused'  — audio paused regardless of data state (set by P
    //               while audio was playing).
    if (state.audio.playState === undefined) state.audio.playState = 'sync';

    function audioShouldPlay() {
        if (state.audio.playState === 'playing') return true;
        if (state.audio.playState === 'paused')  return false;
        return !!messageBus.isPlaying;  // 'sync'
    }

    function syncAudio() {
        const audio = state.audio.element;
        if (!audio || !state.audio.isReady) return;
        adbg('sync: enter playState=', state.audio.playState, 'shouldPlay=', audioShouldPlay());

        if (!audioShouldPlay()) {
            if (!audio.paused) audio.pause();
            updateAudioPlayButton();
            return;
        }

        const speed = messageBus.speed || 1;
        if (speed !== 1 && state.audio.playState === 'sync') {
            // Non-1x replay speed — audio can't match; pause it.
            if (!audio.paused) audio.pause();
            updateAudioPlayButton();
            return;
        }

        // In 'sync', keep the audio gently pinned to the data clock via the
        // ONE seek primitive (soft mode: corrects only sustained drift, so the
        // 60fps clock:update stream can't thrash it). 'gap' = no audio content
        // here → the primitive paused the element; reflect that and bail.
        if (state.audio.playState === 'sync'
                && state.audio.startUtc && messageBus.clockTime) {
            if (placeAudioAtClock(messageBus.clockTime.getTime(), false) === 'gap') {
                updateAudioPlayButton();
                return;
            }
        }

        if (audio.paused) {
            audio.play().catch(e => {
                if (e.name === 'NotAllowedError') {
                    // Autoplay policy — arm the once-only unlock so
                    // the next user click/keydown lets us through.
                    armAudioUnlock();
                } else if (e.name !== 'AbortError') {
                    console.log('Audio play blocked:', e.name);
                }
            });
        }
        updateAudioPlayButton();
    }

    // Reflect the current audio delay in the Delay input box (ss.SSS), unless
    // the user is mid-edit (focused).
    function updateAudioDelayInput() {
        const el = document.getElementById('audioDelayInput');
        if (!el || el === document.activeElement) return;
        el.value = (state.audio.offsetSeconds || 0).toFixed(3);
    }

    // Reflect audio playState on the button (green when playing).
    function updateAudioPlayButton() {
        const btn = document.getElementById('audioPlayBtn');
        const icon = document.getElementById('audioPlayIcon');
        if (!btn || !icon) return;
        const playing = audioShouldPlay();
        btn.classList.toggle('playing', playing);
        icon.innerHTML = playing ? '&#10074;&#10074;' : '&#9658;';
    }

    // Traffic light updater — runs each animation frame from
    // updateClocks. Green = audio actually playing AND has data ready;
    // yellow = seeking or buffering; red = outside the audio window
    // (no startUtc, target before audio start, or past audio end).
    // Audio status light (card HCx1JC3f), user-defined semantics:
    //   GREEN   — audio exists, playing, AND in sync (|drift| ≤ SYNC_DRIFT_S).
    //   YELLOW  — audio exists, playing/loading, but NOT in sync yet
    //             (drifted, or buffering/seeking/starting).
    //   RED     — audio should be playing but is unavailable / out of range / corrupt.
    //   (off)   — playback is deliberately paused (user or data) — not a failure.
    const SYNC_DRIFT_S = 5;   // |audio − data clock| within this = in sync
    function updateAudioStatusLight() {
        const light = document.getElementById('audioStatusLight');
        if (!light) return;
        const audio = state.audio.element;
        let cls = '';

        if (!audioShouldPlay()) {
            cls = '';   // paused on purpose (user or data) → no light
        } else if (!audio || !state.audio.isReady || !state.audio.startUtc
                   || !messageBus.clockTime) {
            cls = 'red';   // should be playing but the audio stream is missing / not ready
        } else if (clockToAudioSec(messageBus.clockTime.getTime()) === null) {
            // No audio CONTENT at this position — before the audio window, an
            // inter-segment gap, or past the end of the file. Benign (not a
            // failure) → no light (HCx1JC3f + end-of-file refinement).
            cls = '';
        } else if (audio.paused || audio.seeking || audio.readyState < 3 /*HAVE_FUTURE_DATA*/) {
            cls = 'yellow';   // wants to play, loading/seeking/not started → not synced yet
        } else {
            const drift = audioSyncDrift();
            const inSync = drift !== null && Math.abs(drift) <= SYNC_DRIFT_S;
            cls = inSync ? 'green' : 'yellow';   // playing: green if synced, else drifted
        }
        if (light.dataset.cls !== cls) {
            light.classList.remove('green', 'yellow', 'red');
            if (cls) light.classList.add(cls);
            light.dataset.cls = cls;
        }
    }

    // =========================================================================
    // Audio UI
    // =========================================================================

    // Called by Space (via base.js togglePlayPause) — resets audio's
    // play state to 'sync' so audio follows the new data state.
    window.resetAudioToSync = function() {
        state.audio.playState = 'sync';
        // syncAudio will be called on the next clock:update / playback
        // status; force one now so the audio element flips immediately.
        syncAudio();
    };

    // (skipAudioRelative removed — a data seek now drags the audio along
    // atomically via placeAudioAtClock on state:seek-complete, so the arrow /
    // + fine-tune keys no longer need a separate audio-only skip. B05)

    window.toggleMute = function() {
        state.audio.isMuted = !state.audio.isMuted;
        // Not persisted — every session opens muted (card 76).
        if (state.audio.element) {
            state.audio.element.volume = state.audio.isMuted ? 0 : state.audio.volume;
        }
        updateMuteButton();
    };

    // Volume slider (0..100 → audio.volume 0..1). Persists to
     // localStorage. When muted, the slider still updates the stored
     // volume but the audio element stays at 0 (so unmute restores it).
    window.handleVolumeChange = function(val) {
        const v = Math.max(0, Math.min(100, parseInt(val, 10) || 0));
        state.audio.volume = v / 100;
        localStorage.setItem('audioVolume', String(v));
        if (state.audio.element && !state.audio.isMuted) {
            state.audio.element.volume = state.audio.volume;
        }
    };

    // (setupAudioScrubber removed — no scrubber DOM to bind.)

    function updateMuteButton() {
        const btn = document.getElementById('muteBtn');
        if (btn) {
            btn.classList.toggle('muted', state.audio.isMuted);
        }
    }

    // =========================================================================
    // Session Loading
    // =========================================================================

    function handleSessionLoaded(data) {
        if (data.duration) {
            state.duration = data.duration;
        }
        if (data.audioInfo) {
            initAudio(data.audioInfo);
        }
        if (data.events) {
            state.events = data.events;
        }
        // Set the transport controls now isLive is known (speed vs LIVE button).
        updateGoLiveButton();
        // Start live tracking (will auto-stop when the chequered flag flies)
        startLiveTracking();
    }

    function handleSessionEvents(events) {
        if (!Array.isArray(events)) return;
        state.events = events;
        state.eventOffsets = new Set(events.map(e => `${e.offset_ms}:${e.topic}:${JSON.stringify(e.data)}`));
        // Via the shared gate: the full events list arrives as a post-restore extra
        // (session:events), so folding it into the restore-done flush keeps the
        // scrubber markers in the single instantaneous seek paint. (SOJffVd3)
        messageBus.scheduleRender('eventMarkers', () => renderEventMarkers(state.events));
    }

    function handleStreamProgress(data) {
        if (data.duration) {
            state.duration = data.duration;
            renderEventMarkers(state.events);
            updateScrubberPosition();   // dot pct depends on the (grown) coordinate system
        }
    }

    // =========================================================================
    // Subscribe
    // =========================================================================

    messageBus.on('sessionInfo', handleSessionInfo);
    messageBus.on('meetingName', handleMeetingName);
    messageBus.on('sessionBadge', handleSessionBadge);
    messageBus.on('raceLaps', (data, offset_ms) => {
        if (!data) return;
        if (data.currentLap != null) {
            state.raceCurrentLap = data.currentLap;
            // Record each lap's start offset (SYNC TO). Skip Lap 1: it has no
            // real LapCount delta — the feed emits currentLap=1 as a pre-race
            // keyframe ~an hour early (offset ~0), which would wrongly pin Lap 1
            // to the session start. Lap 1's start = lights-out, set in
            // handleSessionInfo. (86BYppiU)
            if (offset_ms != null && data.currentLap >= 2
                    && _lapOffset[data.currentLap] == null) {
                _lapOffset[data.currentLap] = offset_ms;
            }
        }
        if (data.totalLaps != null) state.raceTotalLaps = data.totalLaps;
        renderSessionBadge();
    });
    messageBus.on('clock', handleClock);
    messageBus.on('trackStatus', handleTrackStatus);
    messageBus.on('session:loaded', handleSessionLoaded);
    messageBus.on('session:events', handleSessionEvents);
    messageBus.on('stream:progress', handleStreamProgress);
    messageBus.on('playback:status', handlePlaybackStatus);
    messageBus.on('clock:update', handleClockUpdate);

    // Track max received offset for live mode (via clock:update which fires every tick)
    messageBus.on('clock:update', (data) => {
        if (data.offset > state.maxReceivedOffset) {
            state.maxReceivedOffset = data.offset;
        }
    });

    // Live events: only add if offset is beyond all existing markers
    function addLiveEvent(topic, data, payloadOffsetMs) {
        // Stamp the marker with the PAYLOAD offset_ms (ms from session start),
        // not the current playback clock — otherwise a seek/restore re-emit places
        // the marker at the pre-seek clock position instead of the event's real
        // one. Fall back to the clock only for a genuinely-live emission with no
        // payload offset. (hQWTdFtn)
        const offset_ms = (typeof payloadOffsetMs === 'number')
            ? payloadOffsetMs
            : Math.round(messageBus.getCurrentOffset() * 1000);
        const maxOffset = state.events.length > 0
            ? Math.max(...state.events.map(e => e.offset_ms))
            : -1;
        if (offset_ms <= maxOffset) return;
        state.events.push({ offset_ms, topic, data });
        messageBus.scheduleRender('eventMarkers', () => renderEventMarkers(state.events));
    }

    messageBus.on('event', (data, offset_ms) => addLiveEvent('event', data, offset_ms));

    // Audio sync on clock updates
    messageBus.on('clock:update', syncAudio);
    messageBus.on('playback:status', syncAudio);

    // Unlock audio — Firefox/Chrome reject `audio.play()` with
    // NotAllowedError until the user has interacted with the page.
    // Register a once-only global click listener so the FIRST click
    // anywhere wakes the audio up and re-aligns to the data clock.
    let audioUnlockArmed = false;
    function armAudioUnlock() {
        if (audioUnlockArmed) return;
        audioUnlockArmed = true;
        const handler = () => {
            audioUnlockArmed = false;
            if (!state.audio.element) return;
            state.audio.element.play().then(() => {
                if (!messageBus.isPlaying && state.audio.playState !== 'playing') {
                    state.audio.element.pause();
                } else {
                    // Force a drift-corrective resync so the audio
                    // jumps to the current data clock target instead
                    // of continuing from the byte the file loaded at.
                    syncAudio();
                }
            }).catch(() => {});
        };
        // pointerdown also catches taps on touch devices.
        document.addEventListener('click', handler, { once: true, capture: true });
        document.addEventListener('keydown', handler, { once: true, capture: true });
    }

    function tryUnlockAudio() {
        if (!state.audio.element) return;
        state.audio.element.play().then(() => {
            if (!messageBus.isPlaying) state.audio.element.pause();
        }).catch(() => {
            armAudioUnlock();
        });
    }

    messageBus.on('playback:status', (data) => {
        if (data.isPlaying) tryUnlockAudio();
    });

    // The sole post-seek audio move (B05). The clock is already advanced to
    // the target (base.js sets it before emitting), so place the audio there
    // in ONE hard seek via the primitive, then reconcile play/pause. Report the
    // outcome: seek:finished when audio landed (or the session has no audio),
    // seek:failed when the target has no audio content (pre-session / gap /
    // past end). This replaces alignAudioToClock + the settle-guard.
    messageBus.on('state:seek-complete', () => {
        let placed = 'unready';
        if (state.audio.isReady && messageBus.clockTime) {
            placed = placeAudioAtClock(messageBus.clockTime.getTime(), true);
        }
        syncAudio();
        messageBus.emit(placed === 'gap' ? 'seek:failed' : 'seek:finished', {});
    });

    messageBus.on('state:reset', () => {
        // Badge lap-counter state is rebuilt from the restored sessionInfo /
        // raceLaps topics after a seek; clear it so a seek back to pre-race
        // shows the R/S badge again.
        state.raceStarted = false;
        state.raceCurrentLap = null;
        state.raceTotalLaps = null;
        renderSessionBadge();
    });

    // Session time is offset-based (see updateClocks) — it re-derives
    // itself from the playback offset after a seek, so no state:seek-complete
    // adjustment is needed here.

    // ── Team radio (card 8) ──────────────────────────────────────────────
    // Shared player: play a clip while MUTING the commentary, restoring it after.
    // The race-control tile's play buttons call window.playTeamRadio(file); the
    // teamRadio event auto-plays only when enabled in settings (card 27).
    let _radioEl = null;
    function _restoreCommentary() {
        const comm = state.audio.element;
        if (comm) comm.volume = state.audio.isMuted ? 0 : state.audio.volume;
    }
    function playTeamRadio(file) {
        if (!file) return;
        const sess = new URLSearchParams(window.location.search).get('session');
        if (!sess) return;
        if (!_radioEl) { _radioEl = new Audio(); _radioEl.preload = 'auto'; }
        const comm = state.audio.element;
        if (comm) comm.volume = 0;   // mute commentary while the radio plays
        _radioEl.src = `/api/v1/livetiming/teamradio/${encodeURIComponent(sess)}/${encodeURIComponent(file)}`;
        _radioEl.volume = state.audio.isMuted ? 0 : 1.0;
        _radioEl.onended = _restoreCommentary;
        _radioEl.onerror = _restoreCommentary;
        _radioEl.play().catch(_restoreCommentary);
    }
    function stopTeamRadio() {
        if (_radioEl) { _radioEl.pause(); try { _radioEl.currentTime = 0; } catch (e) { /* noop */ } }
        _restoreCommentary();
    }
    window.playTeamRadio = playTeamRadio;
    window.stopTeamRadio = stopTeamRadio;

    // Auto-play on the live event — gated by settings (card 27), and suppressed
    // during a seek-restore (those are history replays, not live airings) and at
    // non-1x speeds. Default OFF until the settings dialog provides the toggle.
    let _radioRestoring = false;
    // On a seek, suppress replayed autoplay AND silence any clip that was already
    // airing — it belongs to the pre-seek position and is holding commentary muted
    // until it ends. stopTeamRadio restores the commentary volume; it's a no-op
    // when nothing is playing (initial connect). (H2YvpH5X)
    messageBus.on('state:reset', () => { _radioRestoring = true; stopTeamRadio(); });
    // Clear on state:restore-done (AFTER the teamRadio history replay), not
    // seek-complete which fires before it — else a seek autoplays a historical
    // clip. Relies on SOJffVd3's terminal marker. (H2YvpH5X)
    messageBus.on('state:restore-done', () => { _radioRestoring = false; });
    messageBus.on('teamRadio', (data) => {
        if (_radioRestoring || !data || !data.file) return;
        if (!messageBus.isPlaying || (messageBus.speed && messageBus.speed !== 1)) return;
        const autoplay = !!(window.F1_SETTINGS && window.F1_SETTINGS.teamRadioAutoplay);
        if (autoplay) playTeamRadio(data.file);
    });

    // Speed button
    document.addEventListener('DOMContentLoaded', () => {
        const speedBtn = document.getElementById('speedBtn');
        if (speedBtn) {
            speedBtn.addEventListener('click', cycleSpeed);
        }
        initScrubber();
    });

})();
