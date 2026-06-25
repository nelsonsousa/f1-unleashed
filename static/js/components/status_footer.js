/* Status footer (card 20) — client health monitoring at the window bottom.
 *
 * Shows: live/replay mode, stream throughput (msg/s) + traffic light, total
 * messages, on-disk cache size, and a data-health light that flags a possible
 * outage when the data stream stalls during playback.
 *
 * Everything except the cache size (which rides in on state:full →
 * session:loaded) is computed client-side from the message bus wildcard.
 */
(function () {
    const $ = (id) => document.getElementById(id);
    if (!$('statusFooter')) return;

    // Internal/control topics that aren't F1 data — excluded from counts/rate.
    const INTERNAL = ['state:', 'session:', 'playback:', 'clock:', 'stream:'];
    const isData = (t) => t && !INTERNAL.some((p) => t.startsWith(p));

    let total = 0;          // total data messages received
    let windowCount = 0;    // data messages since the last rate tick
    let lastDataTs = 0;     // perf-time of the last data message (recency)

    function fmtBytes(b) {
        if (!b) return '—';
        const u = ['B', 'KB', 'MB', 'GB'];
        let i = 0, v = b;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
    }
    const light = (el, cls) => { if (el) el.className = 'sf-light ' + cls; };

    messageBus.on('session:loaded', (d) => {
        const live = !!(d && d.isLive);
        $('sfMode').textContent = live ? 'LIVE' : 'REPLAY';
        $('sfModeDot').className = 'sf-dot ' + (live ? 'live' : 'replay');
        $('sfCache').textContent = fmtBytes(d && d.cacheBytes);
    });

    messageBus.on('*', (topic) => {
        if (!isData(topic)) return;
        total++;
        windowCount++;
        lastDataTs = performance.now();
    });

    let lastTick = performance.now();
    setInterval(() => {
        const now = performance.now();
        const dt = (now - lastTick) / 1000;
        lastTick = now;
        const rate = dt > 0 ? windowCount / dt : 0;
        windowCount = 0;

        $('sfMsgs').textContent = total.toLocaleString();
        $('sfRate').textContent = `${rate.toFixed(rate < 10 ? 1 : 0)}/s`;

        const playing = messageBus.isPlaying;
        // Stream light — throughput health.
        if (!playing) light($('sfStreamLight'), 'grey');
        else if (rate >= 5) light($('sfStreamLight'), 'green');
        else if (rate > 0) light($('sfStreamLight'), 'yellow');
        else light($('sfStreamLight'), 'red');

        // Data light — recency of the data stream; a long stall while playing
        // flags a possible outage.
        const silent = now - lastDataTs;
        const outEl = $('sfOutageLight');
        if (!playing || !lastDataTs) { light(outEl, 'grey'); $('sfOutage').textContent = '—'; }
        else if (silent < 3000) { light(outEl, 'green'); $('sfOutage').textContent = 'OK'; }
        else if (silent < 8000) { light(outEl, 'yellow'); $('sfOutage').textContent = 'sparse'; }
        else { light(outEl, 'red'); $('sfOutage').textContent = 'outage?'; }
    }, 1000);
})();
