/**
 * Track Map Tile
 *
 * Listens to:
 *   - trackCircuit: load the track SVG (server-normalised circuit name)
 *   - position: car positions {num: [x, y, distPct]}
 *   - driverList: driver identity (tla, teamColour)
 *   - trackStatus: flash track on green/red/sc/vsc
 *   - yellowFlag: highlight sectors under yellow
 */

(function() {
    const state = {
        trackSvg: null,
        carMarkersGroup: null,
        carMarkers: {},
        driverInfo: {},      // num -> {tla, color}
        driverStatus: {},    // num -> "RET"|"STOP"|... (card 55: hide RET/STOP markers)
        scale: 1,
        rotation: 0,
        markerRadius: 50,
        markerFontSize: 40,
        markerStrokeWidth: 8,
        location: null,
        lastFlashColour: null,
    };

    const TARGET_MARKER_RADIUS_PX = 12.5;   // +25% (card 70); font tracks radius (0.9×)

    // =========================================================================
    // Track Loading
    // =========================================================================

    function normalizeLocation(name) {
        return name.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, '_');
    }

    async function loadTrackSvg(location) {
        state.location = location;
        const mappedName = window.CircuitUtils?.getSvgFilenameSync(location);
        const filename = mappedName || normalizeLocation(location);
        const svgUrl = `/static/images/tracks/${filename}.svg`;

        try {
            const response = await fetch(svgUrl);
            if (!response.ok) return;

            const svgText = await response.text();
            const parser = new DOMParser();
            const svgDoc = parser.parseFromString(svgText, 'image/svg+xml');
            const svgElement = svgDoc.querySelector('svg');
            if (!svgElement) return;

            const trackRoot = svgElement.querySelector('#track-root');
            if (trackRoot) {
                state.scale = parseFloat(trackRoot.dataset.scale) || 0.05;
                state.rotation = parseFloat(trackRoot.dataset.rotation) || 0;
            }

            state.carMarkersGroup = svgElement.querySelector('#car-markers');
            if (state.carMarkersGroup) {
                // Click a car → focus that driver (+ neighbours in a race) on the dashboard.
                state.carMarkersGroup.addEventListener('click', (e) => {
                    const m = e.target.closest('.car-marker');
                    if (m && window.F1Dashboard) window.F1Dashboard.focus(m.dataset.driver);
                });
            }

            const trackMap = document.getElementById('trackMap');
            if (trackMap) {
                trackMap.innerHTML = '';
                trackMap.appendChild(svgElement);
                state.trackSvg = svgElement;

                const viewBox = svgElement.getAttribute('viewBox');
                if (viewBox) {
                    const vbWidth = parseFloat(viewBox.split(/\s+/)[2]);
                    const renderedWidth = svgElement.getBoundingClientRect().width;
                    const f1PerPx = vbWidth / (renderedWidth * state.scale);

                    state.markerRadius = TARGET_MARKER_RADIUS_PX * f1PerPx;
                    state.markerFontSize = (TARGET_MARKER_RADIUS_PX * 0.9) * f1PerPx;
                    state.markerStrokeWidth = f1PerPx;

                    svgElement.querySelectorAll('.track-outline').forEach(el => {
                        el.setAttribute('stroke-width', (6 * f1PerPx).toFixed(1));
                    });
                    svgElement.querySelectorAll('.track').forEach(el => {
                        el.setAttribute('stroke-width', (2 * f1PerPx).toFixed(1));
                    });
                }

                const calibrating = document.getElementById('trackCalibrating');
                if (calibrating) calibrating.style.display = 'none';

                if (_mini.pending) mountMini(_mini.pending);   // dashboard requested it before load
            }
        } catch (e) {
            console.warn('Failed to load track SVG:', e);
        }
    }

    // =========================================================================
    // Car Markers
    // =========================================================================

    function getContrastColor(hex) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#000' : '#fff';
    }

    function removeCarMarker(num) {
        const marker = state.carMarkers[num];
        if (marker) {
            marker.remove();
            delete state.carMarkers[num];
        }
    }

    function updateCarMarker(num, x, y) {
        // Card 55: a retired / stopped car has no marker on the map. Drop any
        // existing one and ignore further position updates for it.
        const status = state.driverStatus[num];
        if (status === 'RET' || status === 'STOP') {
            removeCarMarker(num);
            return;
        }
        const info = state.driverInfo[num] || {};
        const color = info.color || DEFAULT_CAR_COLOR;
        const tla = info.tla || num;

        let marker = state.carMarkers[num];
        if (!marker && state.carMarkersGroup) {
            marker = createCarMarker(num, color, tla);
            state.carMarkers[num] = marker;
            state.carMarkersGroup.appendChild(marker);
        }
        if (marker) {
            marker.setAttribute('transform', `translate(${x.toFixed(1)}, ${y.toFixed(1)})`);
        }
    }

    function createCarMarker(num, color, tla) {
        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('class', 'car-marker');
        g.setAttribute('data-driver', num);
        g.style.cursor = 'pointer';

        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('r', state.markerRadius);
        circle.setAttribute('fill', color);
        circle.setAttribute('stroke', '#ffffff');
        circle.setAttribute('stroke-width', state.markerStrokeWidth);
        g.appendChild(circle);

        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('fill', getContrastColor(color));
        text.setAttribute('font-size', state.markerFontSize);
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('dominant-baseline', 'central');
        text.setAttribute('transform', `scale(1, -1) rotate(${state.rotation || 0})`);
        text.textContent = tla;
        g.appendChild(text);

        return g;
    }

    // =========================================================================
    // Mini-map — zoomed clone of the track map for the race dashboard (card J3V1CFdS)
    // =========================================================================
    // A cloned track SVG whose viewBox is a zoomed window that follows the "chaser" driver, so it
    // stays centered on them while showing every nearby car marker + the same yellow-flag/track-
    // status colouring as the main map (mirrored each frame). Only the markers are drawn smaller so
    // they don't blow up with the zoom. No rotation, no weather overlay.
    const MINI_ZOOM = 10;              // viewBox zoom (crop = trackViewBox / MINI_ZOOM)
    const MINI_MARKER_SCALE = 0.3;     // marker size (F1 radius)
    const MINI_SMOOTH = 0.18;          // viewBox-centre low-pass (0..1) — damps GPS jitter at high zoom
    const _mini = { svg: null, group: null, container: null, markers: {}, focus: null,
                    baseVB: null, matrix: null, pending: null, sm: {} };

    function mountMini(container) {
        if (!container) return;
        if (!state.trackSvg) { _mini.pending = container; return; }   // SVG not loaded yet → retry on load
        _mini.pending = null;
        const svg = state.trackSvg.cloneNode(true);
        const group = svg.querySelector('#car-markers');
        if (group) { group.innerHTML = ''; group.style.pointerEvents = 'none'; }
        container.innerHTML = ''; container.appendChild(svg);
        const vb = (state.trackSvg.getAttribute('viewBox') || '0 0 100 100').split(/\s+/).map(Number);
        const root = svg.querySelector('#track-root');
        _mini.svg = svg; _mini.group = group; _mini.container = container; _mini.markers = {};
        _mini.baseVB = { w: vb[2], h: vb[3] };
        // Constant F1-coords → SVG-user-space matrix (track-root transform); markers are direct
        // children of track-root, so this maps a car's (x,y) to the viewBox coordinate system.
        _mini.matrix = (root && root.transform.baseVal.numberOfItems)
            ? root.transform.baseVal.consolidate().matrix : null;
    }
    function unmountMini() {
        if (_mini.container) _mini.container.innerHTML = '';
        _mini.svg = _mini.group = _mini.container = _mini.matrix = null; _mini.markers = {}; _mini.sm = {};
    }
    function setMiniFocus(num) {
        _mini.focus = num || null;
    }

    function createMiniMarker(color, tla) {
        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('class', 'car-marker');
        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('r', state.markerRadius * MINI_MARKER_SCALE);   // smaller relative to the track
        circle.setAttribute('fill', color);
        circle.setAttribute('stroke', '#ffffff');
        circle.setAttribute('stroke-width', state.markerStrokeWidth * MINI_MARKER_SCALE);
        g.appendChild(circle);
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('fill', getContrastColor(color));
        text.setAttribute('font-size', state.markerFontSize * MINI_MARKER_SCALE);
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('dominant-baseline', 'central');
        text.setAttribute('transform', `scale(1, -1) rotate(${state.rotation || 0})`);
        text.textContent = tla;
        g.appendChild(text);
        return g;
    }
    function removeMiniMarker(num) {
        const m = _mini.markers[num];
        if (m) { m.remove(); delete _mini.markers[num]; }
    }
    function updateMiniMarker(num, x, y) {
        const info = state.driverInfo[num] || {};
        let m = _mini.markers[num];
        if (!m && _mini.group) {
            m = createMiniMarker(info.color || DEFAULT_CAR_COLOR, info.tla || num);
            _mini.markers[num] = m; _mini.group.appendChild(m);
        }
        if (m) m.setAttribute('transform', `translate(${x.toFixed(1)}, ${y.toFixed(1)})`);
    }

    // Low-pass a car's interpolated position into its persistent smoothed point (_mini.sm[num]).
    // At high zoom the GPS sample-boundary kinks magnify into jitter, so EVERY marker is smoothed —
    // not just the focus — otherwise the others shake against the (smooth) focus-anchored frame.
    function miniSmoothed(num, t) {
        const p = interpAt(posBuf[num], t);
        const s = _mini.sm[num];
        if (!s) { _mini.sm[num] = { x: p.x, y: p.y }; return _mini.sm[num]; }
        s.x += MINI_SMOOTH * (p.x - s.x);
        s.y += MINI_SMOOTH * (p.y - s.y);
        return s;
    }

    function renderMini(t) {
        if (!_mini.svg || !_mini.group) return;
        for (const num in posBuf) {
            const buf = posBuf[num]; if (!buf.length) continue;
            const st = state.driverStatus[num];
            if (st === 'RET' || st === 'STOP') { removeMiniMarker(num); delete _mini.sm[num]; continue; }
            const s = miniSmoothed(num, t);
            updateMiniMarker(num, s.x, s.y);
        }
        for (const num in _mini.markers) if (!posBuf[num]) { removeMiniMarker(num); delete _mini.sm[num]; }
        // Follow the chaser: anchor the viewBox on the focus car's SAME smoothed point, so it sits
        // pinned dead-centre (marker + frame read the identical value → zero swim) while every marker
        // shares the low-pass, keeping the whole field steady at high zoom.
        const s = _mini.focus && _mini.sm[_mini.focus];
        if (s && _mini.matrix) {
            const M = _mini.matrix;
            const sx = M.a * s.x + M.c * s.y + M.e;
            const sy = M.b * s.x + M.d * s.y + M.f;
            const w = _mini.baseVB.w / MINI_ZOOM, h = _mini.baseVB.h / MINI_ZOOM;
            _mini.svg.setAttribute('viewBox',
                `${(sx - w / 2).toFixed(2)} ${(sy - h / 2).toFixed(2)} ${w.toFixed(2)} ${h.toFixed(2)}`);
        }
        syncMiniFlags();
    }

    // Mirror the main map's track-status flash + yellow-flag sectors onto the clone.
    function syncMiniFlags() {
        if (!_mini.svg || !state.trackSvg) return;
        ['#track-outline', '#track-sectors'].forEach(sel => {
            const a = state.trackSvg.querySelector(sel), b = _mini.svg.querySelector(sel);
            if (!a || !b) return;
            b.classList.toggle('flag-blink', a.classList.contains('flag-blink'));
            const col = a.style.getPropertyValue('--flag-color');
            if (col) b.style.setProperty('--flag-color', col);
        });
        const yellow = new Set();
        state.trackSvg.querySelectorAll('[data-sector].sector-yellow')
            .forEach(e => yellow.add(e.getAttribute('data-sector')));
        _mini.svg.querySelectorAll('[data-sector]').forEach(p => {
            p.classList.toggle('sector-yellow', yellow.has(p.getAttribute('data-sector')));
        });
    }

    // =========================================================================
    // Track Flashing
    // =========================================================================

    // Generation counter so a new track-status flash cancels any in-flight
    // blink or solid-hold callbacks left over from a previous one.
    let _flashGen = 0;

    function clearTrackColour() {
        if (!state.trackSvg) return;
        const outline = state.trackSvg.querySelector('#track-outline');
        const sectors = state.trackSvg.querySelector('#track-sectors');
        [outline, sectors].filter(Boolean).forEach(el => el.classList.remove('flag-blink'));
    }

    function flashTrack(color, pulses, onMs, offMs, holdSolid) {
        // Generic blink: `pulses` on-cycles of length `onMs`, each followed by
        // an off-cycle of `offMs`. When `holdSolid` is true the colour is
        // re-applied after the final pulse and LEFT ON (solid) until the next
        // track-status change clears it — keeps the map solid red under a red
        // flag and solid yellow under SC/VSC. (.flag-blink only sets the
        // stroke colour; there's no CSS animation, so leaving it on = solid.)
        if (!state.trackSvg) return;
        const outline = state.trackSvg.querySelector('#track-outline');
        const sectors = state.trackSvg.querySelector('#track-sectors');
        const elements = [outline, sectors].filter(Boolean);
        if (!elements.length) return;

        const gen = ++_flashGen;
        let remaining = pulses;
        const apply = () => {
            if (gen !== _flashGen) return;
            elements.forEach(el => {
                el.style.setProperty('--flag-color', color);
                el.classList.add('flag-blink');
            });
        };
        const clear = () => {
            if (gen !== _flashGen) return;
            elements.forEach(el => el.classList.remove('flag-blink'));
        };
        function on() {
            if (gen !== _flashGen) return;
            if (remaining <= 0) { if (holdSolid) apply(); return; }
            apply();
            setTimeout(off, onMs);
        }
        function off() {
            if (gen !== _flashGen) return;
            clear();
            remaining--;
            if (remaining > 0) setTimeout(on, offMs);
            else if (holdSolid) setTimeout(apply, offMs);
        }
        on();
    }

    // =========================================================================
    // Handlers
    // =========================================================================

    // --- smooth marker motion (position interpolation) ---
    // Markers used to snap to each ~3.7Hz position sample. Instead, buffer samples per
    // car and, each animation frame, render the car at (playback clock − LAG) linearly
    // interpolated between its two bracketing samples — so it glides between them over
    // their real time gap. The small lag guarantees the "next" sample is already here.
    const POS_LAG_MS = 500;
    const posBuf = {};                       // num -> [{t, x, y}] ascending by t (t = ms from session start)

    function handlePosition(data, offsetMs) {
        // The bus passes the message offset (ms from session start) as the 2nd arg — a
        // NUMBER, not a Date (tiles read the live clock from messageBus.clockTime). Use it
        // directly as the sample timestamp; renderNowMs is the same ms-from-start frame.
        if (!data || typeof data !== 'object' || offsetMs == null) return;
        for (const [num, coords] of Object.entries(data)) {
            if (!Array.isArray(coords) || coords.length < 2) continue;
            const buf = posBuf[num] || (posBuf[num] = []);
            if (buf.length && offsetMs <= buf[buf.length - 1].t) continue;   // dup / out-of-order guard
            buf.push({ t: offsetMs, x: coords[0], y: coords[1] });
        }
    }

    function renderNowMs() {
        const ct = messageBus.clockTime, st = messageBus.startTime;
        if (!ct || !st) return null;
        return (ct.getTime() - st.getTime()) - POS_LAG_MS;
    }

    function interpAt(buf, t) {
        if (t <= buf[0].t) return buf[0];
        const last = buf[buf.length - 1];
        if (t >= last.t) return last;                     // clamp — no next sample yet
        for (let i = buf.length - 1; i > 0; i--) {
            const a = buf[i - 1], b = buf[i];
            if (a.t <= t) {
                const f = (t - a.t) / (b.t - a.t);
                return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
            }
        }
        return buf[0];
    }

    function animateMarkers() {
        const t = renderNowMs();
        if (t !== null) {
            for (const num in posBuf) {
                const buf = posBuf[num];
                if (!buf.length) continue;
                const p = interpAt(buf, t);
                updateCarMarker(num, p.x, p.y);
                while (buf.length >= 2 && buf[1].t <= t) buf.shift();   // drop passed, keep the lo bracket
            }
            renderMini(t);   // race dashboard mini-map (no-op unless mounted)
        }
        requestAnimationFrame(animateMarkers);
    }
    requestAnimationFrame(animateMarkers);

    function handleDriverList(data) {
        if (!data || typeof data !== 'object') return;
        for (const [num, info] of Object.entries(data)) {
            state.driverInfo[num] = {
                tla: info.tla || num,
                color: info.color || DEFAULT_CAR_COLOR,
            };
        }
    }

    // Server emits trackStatus {status, message}. Flash once per colour
    // change (the server re-emits on message changes too, e.g. SC DEPLOYED
    // -> SC IN THIS LAP, which maps to the same colour and must not re-flash):
    //   red       → blink red 2×, then SOLID red until green
    //   sc / vsc  → blink yellow 2×, then SOLID yellow until the period ends
    //   green     → blink green 1× (1 s on), clears any solid hold
    // SC↔VSC share the yellow colour, so the guard keeps the solid hold across
    // that transition; chequered/finished/inactive clear the hold (colour=null).
    const FLASH_COLOUR = { green: 'green', red: 'red', sc: 'yellow', vsc: 'yellow' };

    function handleTrackStatus(data) {
        if (!data || typeof data !== 'object') return;
        const colour = FLASH_COLOUR[data.status] || null;
        if (colour === state.lastFlashColour) return;
        state.lastFlashColour = colour;
        // Cancel any in-flight blink / solid-hold and clear the held colour
        // before applying the new status.
        _flashGen++;
        clearTrackColour();
        if (data.status === 'red') {
            flashTrack('#e10600', 2, 500, 500, true);
        } else if (data.status === 'sc' || data.status === 'vsc') {
            flashTrack('#ffd700', 2, 500, 500, true);
        } else if (data.status === 'green') {
            flashTrack('#00ff00', 1, 1000, 0, false);
        }
    }

    function handleYellowFlag(data) {
        if (!state.trackSvg) return;
        // Clear all sector highlights
        state.trackSvg.querySelectorAll('[data-sector]').forEach(p => {
            p.classList.remove('sector-yellow');
        });
        // Highlight flagged sectors
        if (Array.isArray(data)) {
            for (const sector of data) {
                state.trackSvg.querySelectorAll(`[data-sector="${sector}"]`).forEach(p => {
                    p.classList.add('sector-yellow');
                });
            }
        }
    }

    // The circuit comes from the server (SessionInfo.Meeting.Circuit.ShortName,
    // normalised to the SVG basename, e.g. "Monte_Carlo"). On connect/seek
    // the latest trackCircuit row is restored, so this fires before drawing.
    function updatePosWarning(health) {
        const el = document.getElementById('trackPosWarning');
        const msg = document.getElementById('trackPosWarningMsg');
        if (!el || !msg) return;
        const posRed = !!(health && health.position && health.position.level === 'red');
        const telRed = !!(health && health.telemetry && health.telemetry.level === 'red');
        if (posRed && telRed) {
            el.classList.add('red');
            msg.textContent = 'Telemetry and position data unavailable. Data is unreliable';
            el.hidden = false;
        } else if (posRed || telRed) {
            el.classList.remove('red');
            msg.textContent = 'Position data unavailable. Track position estimated from telemetry';
            el.hidden = false;
        } else {
            el.hidden = true;
        }
    }

    messageBus.on('trackCircuit', (name) => {
        if (name && !state.location) {
            loadTrackSvg(name);
        }
    });

    messageBus.on('position', handlePosition);
    messageBus.on('driverList', handleDriverList);
    messageBus.on('trackStatus', handleTrackStatus);
    messageBus.on('yellowFlag', handleYellowFlag);

    // Position-data warning driven by the server's dataHealth: yellow when the GPS
    // OR telemetry feed is down (position is being estimated from telemetry); red
    // when BOTH are down (nothing to estimate from → unreliable).
    messageBus.on('dataHealth', updatePosWarning);
    messageBus.on('state:reset', () => updatePosWarning(null));

    // Driver status — remove a car's marker the moment it retires / stops
    // (card 55). The position feed may keep emitting after RET/STOP, so
    // updateCarMarker also guards on this to avoid re-creating it.
    messageBus.on('driverStatus:', (topic, data) => {
        const num = topic.split(':')[1];
        if (!num) return;
        state.driverStatus[num] = data;
        if (data === 'RET' || data === 'STOP') { removeCarMarker(num); delete posBuf[num]; }
    });

    messageBus.on('state:reset', () => {
        // Clear car markers on seek
        if (state.carMarkersGroup) {
            state.carMarkersGroup.innerHTML = '';
            state.carMarkers = {};
        }
        // Reset status too — restore re-applies it up to the seek point, so a
        // backward seek before a RET/STOP correctly re-shows that car (card 55).
        state.driverStatus = {};
        for (const k in posBuf) delete posBuf[k];
        if (state.trackSvg) {
            state.trackSvg.querySelectorAll('[data-sector]').forEach(p => {
                p.classList.remove('sector-yellow', 'sector-double-yellow');
            });
        }
    });

    // Race dashboard drives a zoomed mini-map instance through this interface (card J3V1CFdS).
    window.F1TrackMap = { mountMini, setMiniFocus, unmountMini };

})();
