/* Audio output picker (debug) — route the commentary <audio> to a chosen output
 * device via HTMLMediaElement.setSinkId, so two clients can each send audio to a
 * DIFFERENT BlackHole virtual device and be recorded simultaneously.
 *
 * Debug-only. Open the panel with f1audioSinkPicker(), or Shift+O when audio
 * debug is enabled (localStorage audioTelemetry|audioSyncDebug = '1'). Console
 * API: f1audioListSinks(), f1audioSetSink(deviceId). The panel picks the sink
 * for THIS client's <audio> (window.f1audioElement, set by header.js).
 *
 * _audioOutputs is pure (browser-free) so it is unit tested; the IIFE no-ops
 * outside a browser. */

function _audioOutputs(devices) {
    return (devices || [])
        .filter(function (d) { return d.kind === 'audiooutput'; })
        .map(function (d, i) {
            return {
                deviceId: d.deviceId,
                label: d.label
                    || ('Output ' + (i + 1) + ' · ' + String(d.deviceId || 'unknown').slice(0, 8)),
            };
        });
}

(function () {
    if (typeof window === 'undefined') return;

    async function listSinks() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
        let outs = _audioOutputs(await navigator.mediaDevices.enumerateDevices());
        // Device labels are hidden until a media permission is granted. If every
        // label is our id-only fallback, request mic permission once to reveal
        // them (debug concession — the track is stopped immediately).
        const idOnly = outs.length && outs.every(function (o) { return /^Output \d+ · /.test(o.label); });
        if (idOnly && navigator.mediaDevices.getUserMedia) {
            try {
                const s = await navigator.mediaDevices.getUserMedia({ audio: true });
                s.getTracks().forEach(function (t) { t.stop(); });
                outs = _audioOutputs(await navigator.mediaDevices.enumerateDevices());
            } catch (e) { /* keep id-only labels */ }
        }
        return outs;
    }
    window.f1audioListSinks = listSinks;

    async function setSink(deviceId) {
        const el = window.f1audioElement || null;
        if (!el) { console.warn('[audio-sink] no audio element yet'); return false; }
        if (typeof el.setSinkId !== 'function') {
            console.warn('[audio-sink] setSinkId is unsupported in this browser');
            return false;
        }
        try {
            await el.setSinkId(deviceId);
            window.f1audioSink = deviceId;
            console.log('[audio-sink] routed audio to', deviceId);
            if (window.f1audioTelemetry) window.f1audioTelemetry.record('setSink', { deviceId: deviceId });
            return true;
        } catch (e) {
            console.warn('[audio-sink] setSinkId failed:', e && e.message);
            return false;
        }
    }
    window.f1audioSetSink = setSink;

    let panel = null;
    function close() { if (panel) { panel.remove(); panel = null; } }

    async function open() {
        close();
        panel = document.createElement('div');
        panel.className = 'audio-sink-picker';
        panel.innerHTML =
            '<div class="asp-title">Audio output</div>'
            + '<div class="asp-list">Loading devices…</div>'
            + '<button type="button" class="asp-close">Close</button>';
        document.body.appendChild(panel);
        panel.querySelector('.asp-close').addEventListener('click', close);

        const list = panel.querySelector('.asp-list');
        const outs = await listSinks();
        if (!panel) return;                       // closed while awaiting
        if (!outs.length) { list.textContent = 'No audio outputs found (or unsupported).'; return; }
        list.textContent = '';
        outs.forEach(function (o) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'asp-device' + (window.f1audioSink === o.deviceId ? ' asp-active' : '');
            b.textContent = o.label;
            b.addEventListener('click', async function () {
                if (await setSink(o.deviceId)) {
                    Array.prototype.forEach.call(list.children, function (c) { c.classList.remove('asp-active'); });
                    b.classList.add('asp-active');
                }
            });
            list.appendChild(b);
        });
    }
    window.f1audioSinkPicker = open;

    // Shift+O opens the picker when audio debug is on (discoverable, not intrusive).
    document.addEventListener('keydown', function (e) {
        if (!e.shiftKey || (e.key !== 'O' && e.key !== 'o')) return;
        let dbg = false;
        try {
            dbg = localStorage.getItem('audioTelemetry') === '1'
                || localStorage.getItem('audioSyncDebug') === '1';
        } catch (x) { dbg = false; }
        if (dbg) { e.preventDefault(); open(); }
    });
})();
