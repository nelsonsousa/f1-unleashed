/* Status footer (card 20) — client health monitoring at the window bottom.
 *
 * Shows: live/replay mode, stream throughput (msg/s) + light, total messages,
 * on-disk cache size, audio bitrate (health), a data-health light that flags a
 * POSITION outage (the high-cardinality location stream going stale vs the
 * playback clock — catches a position drop even while timing keeps flowing),
 * and, for live sessions, data + audio download speeds.
 */
(function () {
    const $ = (id) => document.getElementById(id);
    if (!$('statusFooter')) return;

    // Internal/control topics that aren't F1 data — excluded from counts/rate.
    const INTERNAL = ['state:', 'session:', 'playback:', 'clock:', 'stream:', 'status:'];
    const isData = (t) => t && !INTERNAL.some((p) => t.startsWith(p));

    // Position-staleness thresholds, in DATA-clock ms (speed-independent).
    const POS_OK_MS = 8000;
    const POS_LAG_MS = 20000;

    let total = 0, windowCount = 0;
    let curOffsetMs = 0;          // current playback offset (data clock)
    let lastPosOffsetMs = null;   // data-offset of the last 'position' message

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

    // Position = the high-cardinality location stream (Position.z). Track its
    // data-offset; if it lags the clock the location stream has stalled — an
    // outage — even when timing/other topics keep arriving.
    messageBus.on('position', (data, offset_ms) => {
        if (typeof offset_ms === 'number') lastPosOffsetMs = offset_ms;
    });
    messageBus.on('clock:update', (d) => {
        if (d && typeof d.offset === 'number') curOffsetMs = d.offset * 1000;
    });
    messageBus.on('state:reset', () => { lastPosOffsetMs = null; });

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
        // Stream light — throughput health.
        if (!playing) light($('sfStreamLight'), 'grey');
        else if (rate >= 5) light($('sfStreamLight'), 'green');
        else if (rate > 0) light($('sfStreamLight'), 'yellow');
        else light($('sfStreamLight'), 'red');

        // Data light — POSITION staleness vs the data clock.
        const outEl = $('sfOutageLight');
        if (lastPosOffsetMs === null) {
            light(outEl, 'grey');
            $('sfOutage').textContent = '—';
        } else {
            const lag = curOffsetMs - lastPosOffsetMs;
            if (lag <= POS_OK_MS) { light(outEl, 'green'); $('sfOutage').textContent = 'OK'; }
            else if (lag <= POS_LAG_MS) { light(outEl, 'yellow'); $('sfOutage').textContent = 'lag'; }
            else { light(outEl, 'red'); $('sfOutage').textContent = 'outage'; }
        }
    }, 1000);
})();
