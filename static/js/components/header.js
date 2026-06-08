/**
 * Header Component
 *
 * Listens to processed topics from the server:
 *   sessionInfo    → meeting name, session badge, gmt offset
 *   clock          → UTC time, session time, clock status
 *   trackStatus    → GREEN, RED, SC/VSC messages, CHEQUERED
 *   session:events → scrubber event markers (on initial load)
 *   event          → new event markers during playback
 *   playbackEvent  → sessionStart, sessionEnd
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
            isMuted: localStorage.getItem('audioMuted') === 'true',
            volume: parseFloat(localStorage.getItem('audioVolume') ?? '80') / 100,
            offsetSeconds: 0,        // user-tunable shift (positive → audio plays later)
            decoupled: false,        // when true, syncAudio is suppressed; user controls audio
            seekOffset: 0,           // server-side ?t= seek position (s); added to currentTime when computing displayed-time
        },

        // Animation
        clockAnimId: null,
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
    }

    function handleMeetingName(name) {
        const titleEl = document.getElementById('sessionTitle');
        if (titleEl) titleEl.textContent = name || 'Loading...';
    }

    function handleSessionBadge(badge) {
        const badgeEl = document.getElementById('sessionBadge');
        if (badgeEl) badgeEl.textContent = badge || '--';
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
    function audioPlayableDuration() {
        const audio = state.audio.element;
        if (!audio) return 0;
        if (isFinite(audio.duration) && audio.duration > 0) return audio.duration;
        if (state.duration > 0) return state.duration;
        return 0;
    }

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

    function handleTrackStatus(data) {
        if (!data || typeof data !== 'object') return;

        const color = TRACK_STATUS_COLOR[data.status] || 'white';
        const text = data.message || '--';

        state.trackStatusText = text;
        state.trackStatusColor = color;

        const el = document.getElementById('trackStatus');
        const textEl = document.getElementById('trackStatusText');
        if (el) el.className = `track-status ${color}`;
        if (textEl) textEl.textContent = text;
    }

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
        state.offset = data.offset || 0;
        state.duration = data.duration || state.duration;
        updateScrubberPosition();
        updateGoLiveButton();
        startClockAnimation();
    }

    // "LIVE" button — only meaningful in live mode. Highlights when the
    // clock is at the live edge (within 3 s of duration), so the user
    // can tell at a glance whether they're current or behind.
    function updateGoLiveButton() {
        const btn = document.getElementById('goLiveBtn');
        if (!btn) return;
        const isLiveMode = new URLSearchParams(window.location.search).get('mode') === 'live';
        if (!isLiveMode) {
            btn.classList.add('hidden');
            return;
        }
        btn.classList.remove('hidden');
        const lag = state.duration - state.offset;
        btn.classList.toggle('at-live', lag >= 0 && lag <= 3);
    }

    // Speed button
    const SPEEDS = [1, 2, 5, 10, 30, 50];
    function cycleSpeed() {
        const current = messageBus.speed || 1;
        const idx = SPEEDS.indexOf(current);
        const next = SPEEDS[(idx + 1) % SPEEDS.length];
        messageBus.send({ cmd: 'speed', value: next });
        const btn = document.getElementById('speedBtn');
        if (btn) btn.textContent = `${next}x`;
    }

    // =========================================================================
    // Scrubber
    // =========================================================================

    // Scrubber is non-linear: the session is partitioned into three
    // segments by the PRESTART5MIN and CHEQUERED anchors, with the
    // outer two compressed to ~20 px each so the actual session content
    // (= 5 min before lights-out → chequered) gets the bulk of the
    // visible width.
    //
    //   [ start ─ pre5min ]  [ pre5min ─ chequered ]  [ chequered ─ end ]
    //   ←—— 20 px ——→         ←—— rest of bar ——→     ←—— 20 px ——→
    //
    // When either anchor is absent (= FP/Q with no chequered), falls
    // back to a fully linear [0, duration] mapping.
    //
    // scrubberRange() is kept linear-from-zero for callers that still
    // need the dur_ms; offsetToPct + pctToOffset apply the non-linear
    // mapping.

    const SIDE_TARGET_PX = 20;

    function scrubberRange() {
        return { start_ms: 0, end_ms: state.duration * 1000 };
    }

    function scrubberAnchors() {
        // Section 1 spans [0 → firstEvent_ms], section 3 spans
        // [chequered_ms → duration], section 2 fills the rest.
        //
        // firstEvent is the SPECIFIC "start-of-interesting-content"
        // marker for the session type:
        //   - race / sprint : preStart2min (or preStart5min legacy)
        //   - everything else (= P/Q): sessionStart
        //
        // SessionStart for a race is at offset 0 (= lights-out time),
        // which would collapse section 1 to 0 width. The preStart2min
        // marker is the *lead-in* anchor the user spec'd.
        const dur_ms = state.duration * 1000;
        const scrubberEl = document.getElementById('scrubber');
        const widthPx = scrubberEl && scrubberEl.clientWidth > 0
            ? scrubberEl.clientWidth : 800;
        const sidePct = Math.min(45, (SIDE_TARGET_PX / widthPx) * 100);
        const sessionType = (window.SESSION_CONFIG?.sessionType || '').toLowerCase();
        const isRaceLike = sessionType === 'race' || sessionType === 'sprint';

        // Track:
        //   preStart_ms       — preStart2min (race) or preStart5min (legacy)
        //   chequered_ms      — last chequered flag
        //   firstVisible_ms   — earliest event that ACTUALLY RENDERS as a
        //                       scrubber marker (= sessionStart and
        //                       sessionEnd are filtered out in the
        //                       renderer, so they don't count here).
        let preStart_ms = null, chequered_ms = null, firstVisible_ms = null;
        for (const ev of state.events || []) {
            const d = typeof ev.data === 'string'
                ? ev.data : (ev.data?.event || '');
            const upper = String(d).toUpperCase();
            if (typeof ev.offset_ms !== 'number') continue;
            // Match the renderer's hidden-event set so the anchor lines up
            // with what's actually painted on the strip. AUDIOSTART was
            // retired 2026-06-06 — kept in state.events for legacy data
            // but no longer rendered, so it must NOT count as the first
            // visible event for the scrubber's section-1 boundary.
            const isHidden = upper === 'SESSIONSTART'
                || upper === 'SESSIONEND'
                || upper === 'AUDIOSTART';
            if ((upper === 'PRESTART2MIN' || upper === 'PRESTART5MIN')
                    && preStart_ms === null) {
                preStart_ms = ev.offset_ms;
            }
            if (upper === 'CHEQUERED') {
                if (chequered_ms === null || ev.offset_ms > chequered_ms) {
                    chequered_ms = ev.offset_ms;
                }
            }
            if (!isHidden) {
                if (firstVisible_ms === null
                        || ev.offset_ms < firstVisible_ms) {
                    firstVisible_ms = ev.offset_ms;
                }
            }
        }
        const firstEvent_ms = isRaceLike
            ? (preStart_ms != null ? preStart_ms : firstVisible_ms)
            : firstVisible_ms;
        return {
            start_ms: 0,
            end_ms: dur_ms,
            firstEvent_ms,
            chequered_ms,
            sidePct,
        };
    }

    function offsetToPct(offset_ms) {
        const a = scrubberAnchors();
        if (a.end_ms <= a.start_ms) return 0;
        if (a.firstEvent_ms == null || a.chequered_ms == null
            || a.chequered_ms <= a.firstEvent_ms) {
            const linPct = ((offset_ms - a.start_ms) / (a.end_ms - a.start_ms)) * 100;
            return Math.max(0, Math.min(100, linPct));
        }
        const middlePct = Math.max(0, 100 - 2 * a.sidePct);
        if (offset_ms <= a.firstEvent_ms) {
            const span1 = Math.max(1, a.firstEvent_ms - a.start_ms);
            return ((offset_ms - a.start_ms) / span1) * a.sidePct;
        }
        if (offset_ms <= a.chequered_ms) {
            const span2 = a.chequered_ms - a.firstEvent_ms;
            return a.sidePct + ((offset_ms - a.firstEvent_ms) / span2) * middlePct;
        }
        const span3 = Math.max(1, a.end_ms - a.chequered_ms);
        const after = (offset_ms - a.chequered_ms) / span3;
        return Math.min(100, a.sidePct + middlePct + after * a.sidePct);
    }

    function pctToOffset(pct) {
        const a = scrubberAnchors();
        if (a.end_ms <= a.start_ms) return 0;
        if (a.firstEvent_ms == null || a.chequered_ms == null
            || a.chequered_ms <= a.firstEvent_ms) {
            return Math.max(0, a.start_ms + (pct / 100) * (a.end_ms - a.start_ms));
        }
        const middlePct = Math.max(0, 100 - 2 * a.sidePct);
        if (pct <= a.sidePct) {
            return a.start_ms + (pct / a.sidePct) * (a.firstEvent_ms - a.start_ms);
        }
        if (pct <= a.sidePct + middlePct) {
            const rel = (pct - a.sidePct) / middlePct;
            return a.firstEvent_ms + rel * (a.chequered_ms - a.firstEvent_ms);
        }
        const rel = (pct - a.sidePct - middlePct) / a.sidePct;
        return a.chequered_ms + rel * (a.end_ms - a.chequered_ms);
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
        const hasSessionEnd = events.some(e => {
            const d = typeof e.data === 'string' ? e.data : (e.data?.event || '');
            return d === 'sessionEnd';
        });

        // Each event position uses the non-linear scrubber mapping so
        // the visible scrubber width is dominated by the 5-min-before-
        // start → chequered window. Overlapping flags are allowed (=
        // user-spec'd: 0.5 px dark stroke on each SVG handles the
        // visual separation).
        for (const ev of events) {
            const pct = offsetToPct(ev.offset_ms);
            if (pct < 0 || pct > 100) continue;

            const d = typeof ev.data === 'string' ? ev.data : (ev.data?.event || ev.data || '');
            const upper = String(d).toUpperCase();

            // Skip sessionStart and sessionEnd markers
            if (upper === 'SESSIONSTART' || upper === 'SESSIONEND') continue;

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
                marker = `<div class="scrubber-flag chequered" style="left:${pct}%" title="${title}" ${dataAttrs}>&#127937;</div>`;
            } else if (upper === 'RED' || upper === 'RED FLAG') {
                marker = `<div class="scrubber-flag red" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else if (upper.includes('SC') || upper.includes('VSC') || upper.includes('SAFETY')) {
                marker = `<div class="scrubber-flag yellow" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else if (upper === 'GREEN') {
                marker = `<div class="scrubber-flag green" style="left:${pct}%" title="${title}" ${dataAttrs}>${flagSvg}</div>`;
            } else if (upper === 'PRESTART2MIN') {
                // Stopwatch icon — 2 min before lights out (race/sprint).
                // Marks the start of the playback-scrubber middle section.
                marker = `<div class="scrubber-flag pre-start" style="left:${pct}%" title="2 min to lights out (click to skip to 60 s before)" ${dataAttrs}>`
                       + `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">`
                       + `<path d="M10 2h4"/>`           /* top knob */
                       + `<path d="M12 4v3"/>`           /* stem */
                       + `<circle cx="12" cy="14" r="8" fill="currentColor" fill-opacity="0.1"/>`
                       + `<path d="M12 14L15 11"/>`      /* hand at 2 o'clock */
                       + `</svg></div>`;
            } else if (upper === 'PRESTART5MIN') {
                // Backwards-compat for cached sessions whose live.jsonl
                // was processed before 2026-06-06 (= still has preStart5min).
                marker = `<div class="scrubber-flag pre-start" style="left:${pct}%" title="Pre-session — 5 min before scheduled start (click to skip to 60 s before)" ${dataAttrs}>`
                       + `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">`
                       + `<circle cx="12" cy="12" r="10"/>`
                       + `<text x="12" y="15" text-anchor="middle" font-size="9" font-weight="700" fill="currentColor" stroke="none">5'</text>`
                       + `</svg></div>`;
            } else if (upper === 'AUDIOSTART') {
                continue;   // First-audible marker retired 2026-06-06.
            } else {
                marker = `<div class="scrubber-event" style="left:${pct}%;background:#888" title="${title}" ${dataAttrs}></div>`;
            }

            html += marker;
        }

        // Live button if session end hasn't arrived
        if (!hasSessionEnd) {
            html += `<div class="scrubber-live" id="scrubberLive" title="Jump to live">LIVE</div>`;
        }

        container.innerHTML = html;

        // Bind live button
        const liveBtn = document.getElementById('scrubberLive');
        if (liveBtn) {
            liveBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                seekToOffset(state.duration);
            });
        }
    }

    // Live mode: periodically update duration from latest message offset
    state.liveInterval = null;
    function startLiveTracking() {
        if (state.liveInterval) return;
        state.liveInterval = setInterval(() => {
            // If we have sessionEnd, stop tracking
            const hasEnd = state.events.some(e => {
                const d = typeof e.data === 'string' ? e.data : (e.data?.event || '');
                return d === 'sessionEnd';
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

        // Single endpoint — server decides whether to tail-follow (capture
        // in progress) or serve the static file with range support.
        const audioUrl = `/api/v1/livetiming/audio/${encodeURIComponent(sessionName)}`;
        const audio = new Audio(audioUrl);
        audio.preload = 'auto';
        audio.loop = false;
        state.audio.element = audio;
        // Expose the element so other modules (= tv_sync.js) can read
        // its actual playback position without depending on this IIFE's
        // private state.
        window.f1audioElement = audio;
        // adjustAudioOffset retained as a no-op shim for tv_sync.js;
        // with PDT-anchored audio there's no user-facing offset to
        // adjust anymore.
        window.adjustAudioOffset = function(_deltaSec) {};

        if (audioInfo.start_utc) {
            state.audio.startUtc = new Date(audioInfo.start_utc.replace('Z', '+00:00'));
        }
        state.audio.offsetSeconds = audioInfo.offset_seconds || 0;

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

        // Once we have an initial clock position, align the audio with
        // the data clock so they start together. Required for non-
        // seekable chunked streams (multi-segment replays) where the
        // browser can't seek natively — we ask the server to start the
        // stream from the right byte via ?t=.
        audio.addEventListener('loadedmetadata', alignAudioToClock, { once: true });
        // Also try once we have a confirmed clock; loadedmetadata may
        // not fire reliably on chunked transfer.
        setTimeout(alignAudioToClock, 500);
    }

    // Reload audio at the offset that matches the current data clock.
    // Used on initial load and after seek to keep non-seekable streams
    // in sync (canSeek streams set currentTime in syncAudio instead).
    //
    // If the data clock hasn't reached audio_start_utc yet (capture
    // started LATER than the data feed — common on long pre-shows),
    // pause the audio entirely so it doesn't race ahead. We'll re-fire
    // alignAudioToClock when the data clock catches up.
    function alignAudioToClock() {
        const audio = state.audio.element;
        if (!audio || !state.audio.startUtc || !messageBus.clockTime) return;
        let targetSec = (messageBus.clockTime.getTime() - state.audio.startUtc.getTime()) / 1000
                        - (state.audio.offsetSeconds || 0);
        // Clamp to 0 instead of pausing — a user nudge that pushes
        // target past the start of the audio file should land at
        // byte 0, not be silent. (Live capture: audio resumes.)
        if (targetSec < 0) targetSec = 0;
        const canSeek = audio.seekable && audio.seekable.length > 0
            && audio.seekable.end(audio.seekable.length - 1) > 0;
        if (canSeek) {
            if (Math.abs(audio.currentTime - targetSec) > 1) {
                audio.currentTime = targetSec;
            }
            return;
        }
        // Non-seekable (= live chunked stream). audio.currentTime is
        // "seconds since the most recent fetch's byte 0", NOT "seconds
        // since the file's start". The previous reload set seekOffset =
        // the session-time the new fetch's byte 0 represents. Account
        // for it so drift is measured correctly against the data clock.
        const fileOffset = state.audio.seekOffset || 0;
        const expectedCurrentTime = targetSec - fileOffset;
        const drift = audio.currentTime - expectedCurrentTime;
        // Only reload when drift is significant (≥ 5 s). Each reload
        // tears down + re-fetches the chunked stream and re-buffers,
        // so frequent reloads = the "1-2 s of audio then silent" loop
        // observed during Monaco 2026 live. Real-time clock + audio
        // both advance at 1× and stay in sync without intervention;
        // we only need to correct after a buffer underrun.
        if (Math.abs(drift) > 5) {
            reloadAudioAtOffset(targetSec);
        }
    }

    // For the audio status traffic light: green when audio's playback
    // position is within ±5 s of the data clock, red otherwise. Uses
    // the same seekOffset accounting as alignAudioToClock.
    function audioSyncDrift() {
        const audio = state.audio.element;
        if (!audio || !state.audio.startUtc || !messageBus.clockTime) return null;
        const targetSec = (messageBus.clockTime.getTime() - state.audio.startUtc.getTime()) / 1000
                        - (state.audio.offsetSeconds || 0);
        const fileOffset = state.audio.seekOffset || 0;
        return audio.currentTime - (targetSec - fileOffset);
    }
    window.f1audioSyncDrift = audioSyncDrift;

    function reloadAudioAtOffset(targetSec) {
        const audio = state.audio.element;
        if (!audio) return;
        const intTarget = Math.floor(targetSec);
        // Two debounces:
        //   (a) Skip if same offset (= < 2 s drift since last reload).
        //   (b) THROTTLE to once per 5 s wall-clock. Multiple callers
        //       (loadedmetadata + setTimeout + updateClocks loop) used
        //       to fire reloads back-to-back and abort each other's
        //       fetches mid-flight (NS_BINDING_ABORTED in Firefox);
        //       the prior in-flight reload never delivered any bytes
        //       and audio stayed silent. Force callers to wait so each
        //       fetch has a chance to buffer.
        if (state.audio.seekOffset != null
                && Math.abs(state.audio.seekOffset - intTarget) < 2) {
            return;
        }
        const now = Date.now();
        if (state.audio.lastReloadAt
                && now - state.audio.lastReloadAt < 5000) {
            return;
        }
        state.audio.lastReloadAt = now;
        try {
            const url = new URL(audio.src, window.location.href);
            url.searchParams.set('t', String(intTarget));
            const wasPlaying = !audio.paused && messageBus.isPlaying;
            state.audio.seekOffset = intTarget;
            audio.src = url.toString();
            audio.load();
            if (wasPlaying) audio.play().catch(() => {});
        } catch (e) {
            // URL parse error — give up silently.
        }
    }

    // (Drift poller removed. It was reloading every 5 s in live
    // captures because the buffer underrun made audio.currentTime
    // lag wall time, the poller detected "drift" and reloaded,
    // which restarted buffering, repeat → audio never stable. The
    // ±s nudge buttons let the user resync manually instead.)

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

        // Seek to clock-time only when the underlying response supports
        // byte ranges. Tail-following streams (active live capture)
        // report an empty `seekable` set; in that case just play through.
        const canSeek = audio.seekable && audio.seekable.length > 0
            && audio.seekable.end(audio.seekable.length - 1) > 0;

        if (state.audio.playState === 'sync'
                && state.audio.startUtc && messageBus.clockTime) {
            const clockMs = messageBus.clockTime.getTime();
            const audioStartMs = state.audio.startUtc.getTime();
            // offsetSeconds = data − audio. Positive means audio runs
            // BEHIND data; audio target = clockTarget − offset.
            const targetSec = (clockMs - audioStartMs) / 1000 - (state.audio.offsetSeconds || 0);

            // Past-end-of-file guard: only relevant for SEEKABLE replay
            // streams where audio.duration is the file's true length.
            // For live (= non-seekable, chunked tail-follow) audio.duration
            // is the duration of the CURRENT fetch (= byte 0 of THIS
            // HTTP response), which is much smaller than session-time
            // targetSec — the comparison would always trip "past end"
            // and pause the audio. Skip for non-seekable streams.
            if (targetSec < 0 || (canSeek && isFinite(audio.duration) && targetSec > audio.duration)) {
                if (!audio.paused) audio.pause();
                updateAudioPlayButton();
                return;
            }

            if (canSeek) {
                if (Math.abs(audio.currentTime - targetSec) > 0.5) {
                    audio.currentTime = targetSec;
                }
            } else {
                // Non-seekable (= live chunked). Reload via ?t= when
                // the audible position has drifted > 5 s from the data
                // clock. The OLD gate of "only when actually playing"
                // caused a stuck-paused state during buffer underrun
                // (= F1 Monaco 2026 live): audio paused → no reload →
                // no fresh bytes → still paused, forever. Allow reload
                // when paused too, but throttle to once per 10 s so
                // we don't spam fetches if the server is genuinely
                // slow to produce bytes.
                const playPos = (state.audio.seekOffset || 0) + (audio.currentTime || 0);
                const now = Date.now();
                const sinceLast = now - (state.audio.lastReloadAt || 0);
                if (Math.abs(playPos - targetSec) > 5 && sinceLast > 10000) {
                    state.audio.lastReloadAt = now;
                    reloadAudioAtOffset(targetSec);
                }
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
    function updateAudioStatusLight() {
        const light = document.getElementById('audioStatusLight');
        if (!light) return;
        const audio = state.audio.element;
        let cls = '';
        if (!audio || !state.audio.isReady || !state.audio.startUtc) {
            cls = 'red';
        } else if (messageBus.clockTime) {
            const canSeekL = audio.seekable && audio.seekable.length > 0
                && audio.seekable.end(audio.seekable.length - 1) > 0;
            const targetSec = (messageBus.clockTime.getTime()
                               - state.audio.startUtc.getTime()) / 1000
                              - (state.audio.offsetSeconds || 0);
            const dur = isFinite(audio.duration) && audio.duration > 0
                ? audio.duration : audioPlayableDuration();
            // Past-end-of-file red only applies to SEEKABLE replay
            // streams where audio.duration = full file length. For
            // non-seekable (= live chunked), audio.duration is the
            // CURRENT fetch's length (= much smaller than session
            // targetSec), so the comparison would always trip red.
            // Live is "in range" by definition until the session ends.
            if (targetSec < 0 || (canSeekL && dur > 0 && targetSec > dur)) {
                cls = 'red';
            } else if (audio.seeking || audio.readyState < 3 /*HAVE_FUTURE_DATA*/) {
                cls = 'yellow';
            } else if (audioShouldPlay() && !audio.paused) {
                cls = 'green';
            } else if (audioShouldPlay() && audio.paused) {
                cls = 'yellow';  // wants to play but isn't yet
            } else {
                cls = '';  // user-paused: no light
            }
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

    // Audio-only play/pause (key: P). Toggles between 'playing' and
    // 'paused' states; first invocation from 'sync' switches to the
    // OPPOSITE of audio's current state.
    window.toggleAudioPlay = function() {
        const audio = state.audio.element;
        if (!audio) return;
        const wasPlaying = !audio.paused;
        state.audio.playState = wasPlaying ? 'paused' : 'playing';
        if (wasPlaying) {
            audio.pause();
        } else {
            // If audio wasn't initialised yet, alignAudioToClock will
            // set src + start streaming; otherwise just resume.
            if (state.audio.startUtc) {
                audio.play().catch(() => {});
            }
        }
        updateAudioPlayButton();
        updateAudioStatusLight();
    };

    // Audio-only ±N s skip (called by arrow keys when audio is playing).
    // Doesn't change playState — just shifts the audible position.
    window.skipAudioRelative = function(deltaSeconds) {
        const audio = state.audio.element;
        if (!audio || !state.audio.startUtc) return;
        if (audio.paused && state.audio.playState !== 'playing') return;
        const canSeek = audio.seekable && audio.seekable.length > 0
            && audio.seekable.end(audio.seekable.length - 1) > 0;
        if (canSeek) {
            audio.currentTime = Math.max(0, (audio.currentTime || 0) + deltaSeconds);
        } else {
            const cur = (state.audio.seekOffset || 0) + (audio.currentTime || 0);
            state.audio.seekOffset = null;
            reloadAudioAtOffset(Math.max(0, cur + deltaSeconds));
        }
    };

    // Audio offset input — value in seconds (0.1 s precision). Applied
    // on Enter or focus-out. Positive = data is AHEAD of audio (audio
    // plays content from earlier wall-time relative to the data clock).
    function setupAudioOffsetInput() {
        const inp = document.getElementById('audioOffsetInput');
        if (!inp) return;
        inp.value = (state.audio.offsetSeconds || 0).toFixed(1);
        const apply = () => {
            const v = parseFloat(inp.value);
            if (isNaN(v)) {
                inp.value = (state.audio.offsetSeconds || 0).toFixed(1);
                return;
            }
            state.audio.offsetSeconds = Math.round(v * 10) / 10;
            inp.value = state.audio.offsetSeconds.toFixed(1);
            alignAudioToClock();
        };
        inp.addEventListener('blur', apply);
        inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                inp.blur();
            }
        });
    }

    window.toggleMute = function() {
        state.audio.isMuted = !state.audio.isMuted;
        localStorage.setItem('audioMuted', state.audio.isMuted);
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
        // Start live tracking (will auto-stop when sessionEnd arrives)
        startLiveTracking();
    }

    function handleSessionEvents(events) {
        if (!Array.isArray(events)) return;
        state.events = events;
        state.eventOffsets = new Set(events.map(e => `${e.offset_ms}:${e.topic}:${JSON.stringify(e.data)}`));
        renderEventMarkers(events);
    }

    function handleStreamProgress(data) {
        if (data.duration) {
            state.duration = data.duration;
            renderEventMarkers(state.events);
        }
    }

    // =========================================================================
    // Subscribe
    // =========================================================================

    messageBus.on('sessionInfo', handleSessionInfo);
    messageBus.on('meetingName', handleMeetingName);
    messageBus.on('sessionBadge', handleSessionBadge);
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
    function addLiveEvent(topic, data) {
        const offset_ms = Math.round(messageBus.getCurrentOffset() * 1000);
        const maxOffset = state.events.length > 0
            ? Math.max(...state.events.map(e => e.offset_ms))
            : -1;
        if (offset_ms <= maxOffset) return;
        state.events.push({ offset_ms, topic, data });
        renderEventMarkers(state.events);
    }

    messageBus.on('event', (data) => addLiveEvent('event', data));
    messageBus.on('playbackEvent', (data) => addLiveEvent('playbackEvent', data));

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

    // Force immediate audio resync on seek. Delegates to
    // alignAudioToClock which handles both seekable (native currentTime)
    // and non-seekable (?t= reload + seekOffset tracking) cases.
    messageBus.on('state:seek-complete', alignAudioToClock);

    messageBus.on('state:reset', () => {
    });

    // Session time is offset-based (see updateClocks) — it re-derives
    // itself from the playback offset after a seek, so no state:seek-complete
    // adjustment is needed here.

    // Speed button
    document.addEventListener('DOMContentLoaded', () => {
        const speedBtn = document.getElementById('speedBtn');
        if (speedBtn) {
            speedBtn.addEventListener('click', cycleSpeed);
        }
        initScrubber();
    });

})();
