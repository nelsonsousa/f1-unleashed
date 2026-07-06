/**
 * F1 Live Timing - Base
 *
 * Core functionality:
 * - WebSocket connection to server
 * - Message bus for component communication
 * - Server handles all playback, seeking, and processing
 *
 * Protocol:
 *   state:full     → session metadata + events list (on connect)
 *   state:restore  → latest state per topic (on connect + seek)
 *   state:clock    → playback clock update (offset, duration, speed)
 *   state:status   → playback status (isPlaying, speed)
 *   {topic}        → processed message (e.g. trackStatus, driverTiming:44)
 */

// Configuration
const SESSION_CONFIG = window.SESSION_CONFIG || {
    sessionId: null,
    sessionType: 'qualifying',
    apiBase: '/api/v1/livetiming'
};

const API_BASE = '/api/v1/livetiming';

// Team colors for drivers (2026 season)
const TEAM_COLORS = {
    '1': '#ff8000', '81': '#ff8000',       // McLaren
    '3': '#1e3d7b', '6': '#1e3d7b',        // Red Bull
    '16': '#e8002d', '44': '#e8002d',      // Ferrari
    '12': '#00d4be', '63': '#00d4be',      // Mercedes
    '14': '#1a7a5a', '18': '#1a7a5a',      // Aston Martin
    '10': '#00a1e8', '43': '#00a1e8',      // Alpine
    '23': '#0f4c91', '55': '#0f4c91',      // Williams
    '30': '#2d826d', '41': '#2d826d',      // Racing Bulls
    '31': '#ffffff', '87': '#ffffff',      // Haas
    '5': '#990000', '27': '#990000',       // Audi
    '11': '#6e6e70', '77': '#6e6e70',      // Cadillac
};
const DEFAULT_CAR_COLOR = '#888888';

// =============================================================================
// Message Bus
// =============================================================================

const messageBus = {
    listeners: {},

    // Clock state
    clockTime: null,
    startTime: null,
    endTime: null,
    gmtOffset: null,

    // Playback state
    isPlaying: false,
    finished: false,   // server: feed's terminal SessionStatus=Ends reached
    speed: 1,

    // Seek state
    skipAnimations: false,

    // Streaming state
    streamComplete: false,
    isLive: false,

    // WebSocket
    _ws: null,

    // ==========================================================================
    // Event Bus
    // ==========================================================================

    // Prefix listeners: "driverTiming:" matches "driverTiming:44"
    _prefixListeners: [],

    on(topic, callback) {
        if (topic.endsWith(':')) {
            // Prefix subscription
            this._prefixListeners.push({ prefix: topic, cb: callback });
            return;
        }
        if (!this.listeners[topic]) {
            this.listeners[topic] = [];
        }
        this.listeners[topic].push(callback);
    },

    off(topic, callback) {
        if (topic.endsWith(':')) {
            this._prefixListeners = this._prefixListeners.filter(
                p => !(p.prefix === topic && p.cb === callback)
            );
            return;
        }
        if (this.listeners[topic]) {
            this.listeners[topic] = this.listeners[topic].filter(cb => cb !== callback);
        }
    },

    emit(topic, data, offset_ms) {
        // Exact match
        if (this.listeners[topic]) {
            this.listeners[topic].forEach(cb => cb(data, offset_ms));
        }
        // Prefix match (e.g. "driverTiming:" matches "driverTiming:44")
        for (const p of this._prefixListeners) {
            if (topic.startsWith(p.prefix)) {
                p.cb(topic, data, offset_ms);
            }
        }
        // Wildcard
        if (this.listeners['*']) {
            this.listeners['*'].forEach(cb => cb(topic, data, offset_ms));
        }
    },

    // ==========================================================================
    // Clock Helpers
    // ==========================================================================

    getLocalTime() {
        if (!this.clockTime) return null;
        if (!this.gmtOffset) return this.clockTime;

        const match = this.gmtOffset.match(/^(-?)(\d+):(\d+):(\d+)$/);
        if (!match) return this.clockTime;

        const sign = match[1] === '-' ? -1 : 1;
        const hours = parseInt(match[2]);
        const minutes = parseInt(match[3]);
        const offsetMs = sign * (hours * 3600 + minutes * 60) * 1000;

        return new Date(this.clockTime.getTime() + offsetMs);
    },

    getCurrentOffset() {
        if (!this.clockTime || !this.startTime) return 0;
        return Math.max(0, (this.clockTime.getTime() - this.startTime.getTime()) / 1000);
    },

    getDuration() {
        if (!this.startTime || !this.endTime) return 0;
        return (this.endTime.getTime() - this.startTime.getTime()) / 1000;
    },

    // ==========================================================================
    // WebSocket Connection
    // ==========================================================================

    async connect(sessionName) {
        this.emit('session:loading', { sessionName });

        this.streamComplete = false;
        this.startTime = null;
        this.endTime = null;
        this.clockTime = null;

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const urlMode = new URLSearchParams(window.location.search).get('mode');
        const modeParam = urlMode ? `?mode=${urlMode}` : '';
        const wsUrl = `${protocol}//${window.location.host}/api/v1/livetiming/ws/${encodeURIComponent(sessionName)}${modeParam}`;

        const ws = new WebSocket(wsUrl);
        this._ws = ws;

        ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            const topic = msg.topic;
            const data = msg.data;

            if (topic === 'state:full') {
                // Session metadata from server. isLive is authoritative
                // (server decides live vs replay); the header uses it to
                // swap the speed control for a LIVE button and to hide
                // future events (no-spoiler).
                this.isLive = !!data.isLive;
                if (data.startTime) {
                    this.startTime = new Date(data.startTime);
                }
                if (data.endTime) {
                    this.endTime = new Date(data.endTime);
                }

                this.emit('session:loaded', {
                    sessionType: data.sessionType,
                    isLive: this.isLive,
                    audioInfo: data.audioInfo || null,
                    duration: data.duration,
                    cacheBytes: data.cacheBytes || 0,
                    events: data.events || [],
                });

                // Initialize scrubber
                if (this.startTime && this.endTime) {
                    this.emit('stream:progress', {
                        duration: this.getDuration(),
                    });
                }

                // Emit pre-computed events for scrubber
                if (data.events && data.events.length > 0) {
                    this.emit('session:events', data.events);
                }

                // Set initial playback state
                this.isPlaying = data.isPlaying || false;
                this.speed = data.speed || 1;
                this.finished = !!data.finished;
                this.emit('playback:status', {
                    status: this.isPlaying ? 'playing' : 'paused',
                    isPlaying: this.isPlaying,
                    finished: this.finished,
                });

                // Set initial clock position
                if (data.offset !== undefined && this.startTime) {
                    const displayTime = new Date(this.startTime.getTime() + data.offset * 1000);
                    this.clockTime = displayTime;
                    this.emit('clock:update', {
                        time: displayTime,
                        localTime: this.getLocalTime(),
                        offset: data.offset,
                        duration: data.duration,
                    });
                }

            } else if (topic === 'state:restore') {
                // Full state restore: array of {topic, data, offset_ms}
                this.emit('state:reset', {});
                for (const m of data) {
                    this.emit(m.topic, m.data, m.offset_ms);
                }
                // Advance the display clock to the seek target BEFORE emitting
                // seek-complete, so consumers (audio's alignAudioToClock, tiles)
                // align to the NEW position — `state:clock` arrives a beat later,
                // so without this they'd align to the stale pre-seek clock. (Fix B)
                if (this.startTime && msg.offset_ms != null) {
                    this.clockTime = new Date(this.startTime.getTime() + msg.offset_ms);
                }
                this.emit('state:seek-complete', {
                    offset_ms: msg.offset_ms,
                });

            } else if (topic === 'state:clock') {
                // Clock update from server
                if (this.startTime) {
                    const displayTime = new Date(this.startTime.getTime() + data.offset * 1000);
                    this.clockTime = displayTime;

                    this.emit('clock:update', {
                        time: displayTime,
                        localTime: this.getLocalTime(),
                        offset: data.offset,
                        duration: data.duration,
                    });
                }

            } else if (topic === 'state:status') {
                this.isPlaying = data.isPlaying;
                this.speed = data.speed || 1;
                this.finished = !!data.finished;
                this.emit('playback:status', {
                    status: data.isPlaying ? 'playing' : 'paused',
                    isPlaying: data.isPlaying,
                    finished: this.finished,
                });

            } else if (topic === 'state:stream-progress') {
                if (data.complete) {
                    this.streamComplete = true;
                }
                this.emit('stream:progress', {
                    buffered: data.buffered,
                    duration: data.duration,
                });

            } else if (topic === 'state:scan-progress') {
                this.emit('scan:progress', { pct: data.pct });

            } else if (topic === 'state:events') {
                // Full scrubber-events list, re-broadcast by the server once the
                // transient DB finishes building (replay streams immediately, so
                // the connect-time events list was partial). Re-render markers.
                this.emit('session:events', data || []);

            } else if (topic === 'error') {
                this.emit('error', data);

            } else {
                // All processed messages: trackStatus, driverTiming:44, position, etc.
                this.emit(topic, data, msg.offset_ms);
            }
        };

        ws.onclose = () => {
            console.log('WebSocket closed');
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    },

    // Send command to server
    send(cmd) {
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
            this._ws.send(JSON.stringify(cmd));
        }
    },

    // Clear all state
    reset() {
        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }
        this.clockTime = null;
        this.startTime = null;
        this.endTime = null;
        this.streamComplete = false;
        this.isLive = false;
    },
};

// =============================================================================
// Convenience Functions
// =============================================================================

function togglePlayPause() {
    messageBus.send({ cmd: messageBus.isPlaying ? 'pause' : 'play' });
    // Space re-syncs audio to the new data state — header.js exposes
    // this helper so audio.playState resets to 'sync' (audio follows
    // data again).
    if (typeof window.resetAudioToSync === 'function') {
        window.resetAudioToSync();
    }
}

function seekToOffset(offset) {
    messageBus.send({ cmd: 'seek', offset: offset });
}

function seekLive() {
    messageBus.send({ cmd: 'seek_live' });
}

// =============================================================================
// Keyboard Shortcuts
// =============================================================================

document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

    // Space: toggle BOTH streams together, aligned to the data state.
    // After space, audio.playState is reset to 'sync' (audio follows
    // the new data state). See header.js togglePlayPause + audio block.
    if (e.key === ' ') {
        e.preventDefault();
        togglePlayPause();
    // Enter: jump to the SYNC TO marker (previous sync event shown on the
    // button) and resume playback (if paused).
    } else if (e.key === 'Enter') {
        e.preventDefault();
        const btn = document.getElementById('syncBtn');
        if (btn && btn._syncOffset != null) {
            seekToOffset(btn._syncOffset);
            messageBus.send({ cmd: 'play' });
        }
    // Arrow keys: ±10 s skip. Works whether playing OR paused — a paused
    // seek just repositions the playhead. Data uses the global seek; audio
    // piggy-backs through skipAudioRelative.
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        const delta = e.key === 'ArrowLeft' ? -10 : 10;
        seekToOffset(Math.max(0, messageBus.getCurrentOffset() + delta));
        if (typeof window.skipAudioRelative === 'function') {
            window.skipAudioRelative(delta);
        }
    // + / = : nudge forward 0.5 s (manual sync fine-tune). Modifier-free only,
    // so Cmd/Ctrl + still zooms the browser.
    } else if ((e.key === '+' || e.key === '=') && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        seekToOffset(messageBus.getCurrentOffset() + 0.5);
        if (typeof window.skipAudioRelative === 'function') {
            window.skipAudioRelative(0.5);
        }
    // - : pause 0.1 s then resume — nudges the stream 0.1 s later vs real time
    // (fine-tune). Modifier-free only, so Cmd/Ctrl - still zooms out.
    } else if (e.key === '-' && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        messageBus.send({ cmd: 'pause' });
        setTimeout(() => messageBus.send({ cmd: 'play' }), 100);
    // M: mute toggle (handy without reaching for the mute button).
    } else if (e.key === 'm' || e.key === 'M') {
        e.preventDefault();
        if (typeof window.toggleMute === 'function') window.toggleMute();
    }
});

// =============================================================================
// Auto-initialization
// =============================================================================

document.addEventListener('DOMContentLoaded', async () => {
    if (window.CircuitUtils) {
        await window.CircuitUtils.loadCircuits();
    }

    const params = new URLSearchParams(window.location.search);
    const sessionName = params.get('session');
    const mode = params.get('mode');

    const targetSession = sessionName || SESSION_CONFIG.sessionId;

    if (targetSession) {
        try {
            messageBus.on('session:loaded', () => {
                // Auto-play — use setTimeout to let state:restore arrive first
                setTimeout(() => messageBus.send({ cmd: 'play' }), 100);
            });
            await messageBus.connect(targetSession);
        } catch (error) {
            // Error already handled
        }
    } else {
        messageBus.emit('error', { message: 'No session specified. Use ?session=SESSION_NAME' });
    }
});
