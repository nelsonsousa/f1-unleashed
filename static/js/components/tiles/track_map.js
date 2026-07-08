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
    messageBus.on('trackCircuit', (name) => {
        if (name && !state.location) {
            loadTrackSvg(name);
        }
    });

    messageBus.on('position', handlePosition);
    messageBus.on('driverList', handleDriverList);
    messageBus.on('trackStatus', handleTrackStatus);
    messageBus.on('yellowFlag', handleYellowFlag);

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

})();
