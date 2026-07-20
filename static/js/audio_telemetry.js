/* Audio telemetry recorder (card VOPkIiAh) — opt-in, in-memory ring buffer of
 * timestamped audio-pipeline events, for diagnosing why live audio pauses.
 *
 * Every event carries an absolute wall-clock ms (`t`) so client events line up
 * with the server telemetry AND an external speaker recording on one timeline.
 * Optional data-clock context (`clockMs`/`offset`) is passed in per-event by the
 * caller so an audio stall can be correlated with data-stream bursts (B07).
 *
 * `createAudioTelemetry` is the PURE core — no browser globals — so it is unit
 * tested directly (tests/test_audio_telemetry.mjs). The IIFE at the bottom wires
 * one instance to `window` + the real clock; it no-ops outside a browser. */

function createAudioTelemetry(opts) {
    opts = opts || {};
    const capacity = opts.capacity || 8000;      // ring-buffer cap; oldest dropped
    const now = opts.now || function () { return Date.now(); };
    let enabled = !!opts.enabled;
    let events = [];
    let dropped = 0;                             // count evicted by the cap (honesty)

    function record(type, fields) {
        if (!enabled) return null;
        const ev = { t: now(), type: type };
        if (fields) {
            for (const k in fields) {
                if (Object.prototype.hasOwnProperty.call(fields, k)) ev[k] = fields[k];
            }
        }
        events.push(ev);
        if (events.length > capacity) {
            dropped += events.length - capacity;
            events.splice(0, events.length - capacity);
        }
        return ev;
    }

    return {
        record: record,
        setEnabled: function (v) { enabled = !!v; },
        isEnabled: function () { return enabled; },
        size: function () { return events.length; },
        dropped: function () { return dropped; },
        clear: function () { events = []; dropped = 0; },
        // A defensive COPY so callers can't mutate the internal buffer.
        export: function () { return events.map(function (e) { return Object.assign({}, e); }); },
        toJSON: function () {
            return JSON.stringify({ capacity: capacity, count: events.length, dropped: dropped, events: events });
        },
    };
}

/* ---- browser wiring (no-op under Node / tests) ---- */
(function () {
    if (typeof window === 'undefined') return;
    let on = false;
    try {
        on = localStorage.getItem('audioTelemetry') === '1'
            || localStorage.getItem('audioSyncDebug') === '1';   // reuse the existing debug gate
    } catch (e) { on = false; }

    const tel = createAudioTelemetry({ capacity: 8000, enabled: on });
    window.f1audioTelemetry = tel;

    // Manual export: `f1audioTelemetryDownload()` from the console saves the buffer.
    window.f1audioTelemetryDownload = function () {
        const blob = new Blob([tel.toJSON()], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'audio_telemetry_' + tel.size() + '.json';
        a.click();
        URL.revokeObjectURL(a.href);
    };

    // Offload the timeline to the server telemetry subfolder (needs the server
    // `telemetry` setting on). Beacon so it survives page hide.
    window.f1audioTelemetryBeacon = function () {
        try {
            const sess = (window.SESSION_CONFIG && window.SESSION_CONFIG.sessionId) || 'unknown';
            const url = '/api/v1/telemetry/audio-timeline?session=' + encodeURIComponent(sess);
            const body = tel.toJSON();
            if (navigator.sendBeacon) {
                navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
            } else {
                fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true });
            }
            return true;
        } catch (e) { console.warn('[audio-telemetry] beacon failed', e); return false; }
    };
    window.addEventListener('pagehide', function () {
        if (tel.isEnabled() && tel.size()) window.f1audioTelemetryBeacon();
    });
})();
