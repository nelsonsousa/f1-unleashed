/* Status footer (card 20) — client health monitoring at the window bottom.
 *
 * Shows: live/replay mode, stream throughput (msg/s) + light, total messages,
 * on-disk cache size, audio bitrate (health), a data-health light driven by the
 * server's authoritative `dataHealth` (position stale / carData invalid|missing /
 * TimingData stale, all green-gated — see data_health_processor), and, for live
 * sessions, data + audio download speeds.
 */
(function () {
    const $ = (id) => document.getElementById(id);
    if (!$('statusFooter')) return;

    // Internal/control topics that aren't F1 data — excluded from counts/rate.
    // `heartbeat` is a keep-alive, not data throughput, so it's excluded from
    // the msg/s count but DOES drive the stream light (liveness) below.
    const INTERNAL = ['state:', 'session:', 'playback:', 'clock:', 'stream:', 'status:'];
    const isData = (t) => t && t !== 'heartbeat' && !INTERNAL.some((p) => t.startsWith(p));

    // Stream light = data-feed liveness by Heartbeat recency (~15 s cadence).
    // Pre/post-session the msg/s rate drops to ~0 (only heartbeats arrive) yet
    // the feed is healthy, so the light tracks heartbeat age, not the rate.
    const HB_YELLOW_S = 30;   // no heartbeat this long → yellow
    const HB_RED_S = 60;      // no heartbeat this long → red
    let lastHeartbeatMs = null;

    let total = 0, windowCount = 0;
    let health = null;      // latest dataHealth payload from the server
    let finished = false;   // server: playback parked at the terminal session end
    let isLive = false;     // live vs replay (from session:loaded)
    let streamAlive = true; // server: raw data feed advancing (live stream light)

    function fmtBytes(b) {
        if (!b) return '—';
        const u = ['B', 'KB', 'MB', 'GB'];
        let i = 0, v = b;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
    }
    function fmtRate(bps) {
        if (!bps) return '0 KB/s';
        const kb = bps / 1024;
        return kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB/s` : `${kb.toFixed(kb < 10 ? 1 : 0)} KB/s`;
    }
    const light = (el, cls) => { if (el) el.className = 'sf-light ' + cls; };

    messageBus.on('session:loaded', (d) => {
        const live = !!(d && d.isLive);
        isLive = live;
        $('sfMode').textContent = live ? 'LIVE' : 'REPLAY';
        $('sfModeDot').className = 'sf-dot ' + (live ? 'live' : 'replay');
        $('sfCache').textContent = fmtBytes(d && d.cacheBytes);
        const br = d && d.audioInfo && d.audioInfo.bitrateKbps;
        $('sfAudio').textContent = br ? `${br} kbps` : '—';
        light($('sfAudioLight'), br ? 'green' : 'grey');
        document.querySelectorAll('.sf-live-only').forEach((el) => el.classList.toggle('hidden', !live));
    });

    // Authoritative data health from the server (data_health_processor): three
    // per-stream boxes coloured by the fraction of ON-TRACK drivers affected.
    function setBox(el, info, label) {
        if (!el) return;
        const lvl = (info && info.level) || 'green';
        el.className = 'sf-hbox h-' + lvl;
        const drv = (info && info.drivers) || [];
        if (drv.length) el.title = `${label}: ${drv.join(', ')}`;
        else el.removeAttribute('title');
    }
    function renderHealth() {
        if (finished) {
            // Session ended — a settled state, not a fault. Neutral, never red.
            ['sfhTiming', 'sfhTel', 'sfhPos'].forEach((id) => {
                const el = $(id);
                if (el) { el.className = 'sf-hbox h-off'; el.removeAttribute('title'); }
            });
            return;
        }
        // TIMING is all-or-nothing (green/red): red = the whole feed has stopped.
        const tEl = $('sfhTiming');
        if (tEl) {
            const tRed = !!(health && health.timing && health.timing.level === 'red');
            tEl.className = 'sf-hbox h-' + (tRed ? 'red' : 'green');
            if (tRed) tEl.title = 'TimingData feed stopped'; else tEl.removeAttribute('title');
        }
        setBox($('sfhTel'), health && health.telemetry, 'Telemetry invalid/missing');
        setBox($('sfhPos'), health && health.position, 'Position stale');
    }
    messageBus.on('dataHealth', (d) => { health = d; renderHealth(); });
    messageBus.on('state:reset', () => { health = null; lastHeartbeatMs = null; finished = false; renderHealth(); });
    messageBus.on('heartbeat', () => { lastHeartbeatMs = performance.now(); });
    // Server-authoritative live feed liveness (raw data edge, not the audio-capped
    // playhead) — drives the stream light for LIVE sessions (card Xqw1feac).
    messageBus.on('streamLive', (d) => { streamAlive = !!(d && d.alive); });
    // Server-authoritative terminal-end flag (on state:status / state:full via base.js).
    messageBus.on('playback:status', (d) => {
        const was = finished;
        finished = !!(d && d.finished);
        if (finished !== was) renderHealth();
    });

    messageBus.on('status:rates', (d) => {
        if (!d) return;
        // Cache size grows through the session — refresh it live (card imRSQecj).
        if (d.cacheBytes != null) $('sfCache').textContent = fmtBytes(d.cacheBytes);
        if (finished) return;   // parked at end → the 1 s tick shows '—' speeds
        $('sfDlDataVal').textContent = fmtRate(d.dataBps);
        $('sfDlAudioVal').textContent = fmtRate(d.audioBps);
    });

    messageBus.on('*', (topic) => {
        if (!isData(topic)) return;
        total++;
        windowCount++;
    });

    let lastTick = performance.now();
    setInterval(() => {
        const now = performance.now();
        const dt = (now - lastTick) / 1000;
        lastTick = now;
        const rate = dt > 0 ? windowCount / dt : 0;
        windowCount = 0;

        $('sfMsgs').textContent = total.toLocaleString();

        const streamLight = $('sfStreamLight');
        if (finished) {
            // Session ended: no live feed to rate or monitor. Settled state —
            // '—' speeds + a neutral light, never an alarming 0 / red.
            $('sfRate').textContent = '—';
            light(streamLight, 'grey');
            if (streamLight) streamLight.title = 'Session ended';
            const dd = $('sfDlDataVal'), da = $('sfDlAudioVal');
            if (dd) dd.textContent = '—';
            if (da) da.textContent = '—';
            return;
        }

        $('sfRate').textContent = `${rate.toFixed(rate < 10 ? 1 : 0)} msg/s`;

        if (isLive) {
            // LIVE: server-authoritative feed liveness (raw data edge — heartbeats
            // keep it fresh). NOT client heartbeat-recency, which lags behind the
            // audio-capped playhead and went false-red post-session (card Xqw1feac).
            light(streamLight, streamAlive ? 'green' : 'red');
        } else {
            // REPLAY: playback liveness by delivered-heartbeat recency.
            const playing = messageBus.isPlaying;
            const hbAge = lastHeartbeatMs === null ? null : (now - lastHeartbeatMs) / 1000;
            if (!playing || hbAge === null) light(streamLight, 'grey');
            else if (hbAge <= HB_YELLOW_S) light(streamLight, 'green');
            else if (hbAge <= HB_RED_S) light(streamLight, 'yellow');
            else light(streamLight, 'red');
        }
        if (streamLight) streamLight.removeAttribute('title');
    }, 1000);
})();
