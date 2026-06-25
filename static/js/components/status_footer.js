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
    const INTERNAL = ['state:', 'session:', 'playback:', 'clock:', 'stream:', 'status:'];
    const isData = (t) => t && !INTERNAL.some((p) => t.startsWith(p));

    let total = 0, windowCount = 0;
    let health = null;   // latest dataHealth payload from the server

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
        $('sfMode').textContent = live ? 'LIVE' : 'REPLAY';
        $('sfModeDot').className = 'sf-dot ' + (live ? 'live' : 'replay');
        $('sfCache').textContent = fmtBytes(d && d.cacheBytes);
        const br = d && d.audioInfo && d.audioInfo.bitrateKbps;
        $('sfAudio').textContent = br ? `${br} kbps` : '—';
        light($('sfAudioLight'), br ? 'green' : 'grey');
        document.querySelectorAll('.sf-live-only').forEach((el) => el.classList.toggle('hidden', !live));
    });

    // Authoritative data health from the server (data_health_processor).
    messageBus.on('dataHealth', (d) => { health = d; renderHealth(); });
    messageBus.on('state:reset', () => { health = null; renderHealth(); });

    messageBus.on('status:rates', (d) => {
        if (!d) return;
        $('sfDlDataVal').textContent = fmtRate(d.dataBps);
        $('sfDlAudioVal').textContent = fmtRate(d.audioBps);
    });

    messageBus.on('*', (topic) => {
        if (!isData(topic)) return;
        total++;
        windowCount++;
    });

    function renderHealth() {
        const el = $('sfOutageLight');
        const txt = $('sfOutage');
        if (!health) { light(el, 'grey'); txt.textContent = '—'; txt.removeAttribute('title'); return; }
        const s = health.status;
        light(el, s === 'critical' ? 'red' : s === 'warn' ? 'yellow' : 'green');
        if (s === 'critical') { txt.textContent = 'TIMING'; txt.title = 'TimingData stalled under green'; return; }
        if (s === 'ok') { txt.textContent = 'OK'; txt.removeAttribute('title'); return; }
        const parts = [], tip = [];
        const pos = health.positionStale || [], inv = health.carDataInvalid || [], miss = health.carDataMissing || [];
        if (pos.length) { parts.push('pos ' + pos.length); tip.push('Position stale: ' + pos.join(', ')); }
        if (inv.length) { parts.push('tel ' + inv.length); tip.push('Telemetry invalid: ' + inv.join(', ')); }
        if (miss.length) { parts.push('car ' + miss.length); tip.push('CarData missing: ' + miss.join(', ')); }
        txt.textContent = parts.join('  ') || 'warn';
        txt.title = tip.join('\n');
    }

    let lastTick = performance.now();
    setInterval(() => {
        const now = performance.now();
        const dt = (now - lastTick) / 1000;
        lastTick = now;
        const rate = dt > 0 ? windowCount / dt : 0;
        windowCount = 0;

        $('sfMsgs').textContent = total.toLocaleString();
        $('sfRate').textContent = `${rate.toFixed(rate < 10 ? 1 : 0)} msg/s`;

        const playing = messageBus.isPlaying;
        if (!playing) light($('sfStreamLight'), 'grey');
        else if (rate >= 5) light($('sfStreamLight'), 'green');
        else if (rate > 0) light($('sfStreamLight'), 'yellow');
        else light($('sfStreamLight'), 'red');
    }, 1000);
})();
