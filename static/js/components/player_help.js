/**
 * Player-help modal (release v2.0.0).
 *
 * A "Player help" link on the right of the status footer opens this overlay,
 * which explains every playback control. It is deliberately a client-only
 * overlay: opening it does NOT pause playback, so the help stays readable while
 * the session keeps running underneath. Built on demand, mirroring settings.js.
 *
 * NOTE: the Buy-me-a-coffee URL below is a placeholder — replace the handle.
 */
(function () {
    'use strict';

    const COFFEE_URL = 'https://www.buymeacoffee.com/nsousa';   // TODO: confirm handle

    // Content is a static HTML string — the player controls never change per
    // session, so there is nothing to compute.
    const BODY = `
        <div class="phelp-section">
            <h4>Playback &amp; seeking</h4>
            <div class="phelp-row"><span class="k">Play / pause</span><span class="v">Toggle playback. The session clock keeps the data, audio and visuals in step.</span></div>
            <div class="phelp-row"><span class="k">Scrubber</span><span class="v">Drag anywhere on the bar to seek. Seeks are instant — the full state is rebuilt at the target moment.</span></div>
            <div class="phelp-row"><span class="k">Event markers</span><span class="v">Ticks on the scrubber. Click one to jump to <strong>~60&nbsp;s before</strong> that event: the 2-minute notice, session start, session finish, safety car / VSC, green flags and red flags.</span></div>
            <div class="phelp-row"><span class="k">Speed</span><span class="v"><strong>1×–50×</strong> in replay; locked to <strong>1×</strong> live.</span></div>
            <div class="phelp-row"><span class="k">LIVE</span><span class="v">Live sessions only. Red at the live edge, black when you have rewound — click to snap back to the latest data.</span></div>
        </div>

        <div class="phelp-section">
            <h4>Audio</h4>
            <div class="phelp-row"><span class="k">Mute / volume</span><span class="v">Control the broadcast commentary. Team-radio clips duck the commentary while they play.</span></div>
            <div class="phelp-row"><span class="k">Delay box</span><span class="v"><code>ss.SSS</code> manual offset — positive plays commentary later, negative earlier. Rarely needed: audio is auto-anchored to the data clock.</span></div>
            <div class="phelp-row"><span class="k">Traffic light</span><span class="v"><span class="phelp-dot g"></span>in sync &nbsp; <span class="phelp-dot y"></span>seeking / loading &nbsp; <span class="phelp-dot r"></span>no audio for this moment.</span></div>
        </div>

        <div class="phelp-section">
            <h4>Video sync — align to a TV broadcast</h4>
            <div class="phelp-row"><span class="k">P/Q button</span><span class="v">Screen-share the muted TV once; it reads the on-screen session clock and seeks the data to match.</span></div>
            <div class="phelp-row"><span class="k">Race button</span><span class="v">Watches the lap counter for a few seconds and aligns the data to the lap change. Use once the race is green; click near a lap change.</span></div>
            <div class="phelp-row"><span class="k"><kbd>Enter</kbd></span><span class="v">Jump to the start instant (next green in P/Q; scheduled start or lights-out in the race) and resume if paused.</span></div>
            <div class="phelp-row"><span class="k"><kbd>+</kbd> / <kbd>−</kbd></span><span class="v">Fine nudges once video sync has been used: <kbd>+</kbd> the TV is ahead (data forward ~0.5&nbsp;s); <kbd>−</kbd> the TV is behind (pause ~0.1&nbsp;s to let the picture catch up).</span></div>
        </div>

        <div class="phelp-section">
            <h4>Status bar (this footer)</h4>
            <div class="phelp-row"><span class="k">Mode</span><span class="v">Live or Replay, with a coloured dot.</span></div>
            <div class="phelp-row"><span class="k">Stream / Messages</span><span class="v">Throughput (msg/s) with a health light, and the total message count so far.</span></div>
            <div class="phelp-row"><span class="k">Cache / Audio</span><span class="v">On-disk size of this session and the commentary bitrate.</span></div>
            <div class="phelp-row"><span class="k">Data / Audio buf</span><span class="v">How much data and audio are buffered ahead of the playhead.</span></div>
            <div class="phelp-row"><span class="k">Data health</span><span class="v"><strong>TIMING / TELEMETRY / POSITION</strong> boxes over the cars currently on track: <span class="phelp-dot g"></span>good &nbsp; <span class="phelp-dot y"></span>minor &nbsp; <span class="phelp-dot r"></span>outage. They pause during red / SC / VSC.</span></div>
        </div>

        <div class="phelp-support">
            Enjoying F1&nbsp;Unleashed? You can support the project
            <a href="${COFFEE_URL}" target="_blank" rel="noopener">on Buy&nbsp;me&nbsp;a&nbsp;coffee</a>.
        </div>`;

    function ensureModal() {
        if (document.getElementById('playerHelpModal')) return;
        const m = document.createElement('div');
        m.id = 'playerHelpModal';
        m.className = 'phelp-modal hidden';
        m.innerHTML =
            `<div class="phelp-dialog" role="dialog" aria-modal="false" aria-label="Player help">
                <div class="phelp-head"><h3>Player help</h3><button class="phelp-close" aria-label="Close">&times;</button></div>
                <div class="phelp-body">${BODY}</div>
            </div>`;
        document.body.appendChild(m);
        m.querySelector('.phelp-close').addEventListener('click', close);
        m.addEventListener('click', (e) => { if (e.target === m) close(); });
    }

    function open() {
        ensureModal();
        document.getElementById('playerHelpModal').classList.remove('hidden');
    }
    function close() {
        const m = document.getElementById('playerHelpModal');
        if (m) m.classList.add('hidden');
    }

    window.openPlayerHelp = open;
    document.addEventListener('click', (e) => {
        if (e.target.closest('.open-player-help')) { e.preventDefault(); open(); }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') close();
    });
})();
