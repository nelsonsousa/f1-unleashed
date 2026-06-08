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
        scale: 1,
        rotation: 0,
        markerRadius: 50,
        markerFontSize: 40,
        markerStrokeWidth: 8,
        location: null,
        lastFlashColour: null,
    };

    const TARGET_MARKER_RADIUS_PX = 10;

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

    function updateCarMarker(num, x, y) {
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

    function flashTrack(color, pulses, onMs, offMs) {
        // Generic blink: `pulses` on-cycles of length `onMs`, each
        // followed by an off-cycle of `offMs`. Defaults to 3 × (0.5 s
        // on, 0.5 s off) for red / SC / VSC; GREEN uses 1 × 1 s.
        if (!state.trackSvg) return;
        const outline = state.trackSvg.querySelector('#track-outline');
        const sectors = state.trackSvg.querySelector('#track-sectors');
        const elements = [outline, sectors].filter(Boolean);
        if (!elements.length) return;

        let remaining = pulses;
        function on() {
            if (remaining <= 0) return;
            elements.forEach(el => {
                el.style.setProperty('--flag-color', color);
                el.classList.add('flag-blink');
            });
            setTimeout(off, onMs);
        }
        function off() {
            elements.forEach(el => el.classList.remove('flag-blink'));
            remaining--;
            if (remaining > 0) setTimeout(on, offMs);
        }
        on();
    }

    // =========================================================================
    // Handlers
    // =========================================================================

    function handlePosition(data) {
        if (!data || typeof data !== 'object') return;
        for (const [num, coords] of Object.entries(data)) {
            if (!Array.isArray(coords) || coords.length < 2) continue;
            updateCarMarker(num, coords[0], coords[1]);
        }
    }

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
    // -> SC IN THIS LAP, which must not re-trigger the flash):
    //   red       → blink red 3× (0.5 s on, 0.5 s off)
    //   sc / vsc  → blink yellow 3× (0.5 s on, 0.5 s off)
    //   green     → blink green 1× (1 s on)
    const FLASH_COLOUR = { green: 'green', red: 'red', sc: 'yellow', vsc: 'yellow' };

    function handleTrackStatus(data) {
        if (!data || typeof data !== 'object') return;
        const colour = FLASH_COLOUR[data.status] || null;
        if (colour === state.lastFlashColour) return;
        state.lastFlashColour = colour;
        if (data.status === 'red') {
            flashTrack('#e10600', 3, 500, 500);
        } else if (data.status === 'sc' || data.status === 'vsc') {
            flashTrack('#ffd700', 3, 500, 500);
        } else if (data.status === 'green') {
            flashTrack('#00ff00', 1, 1000, 0);
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

    messageBus.on('state:reset', () => {
        // Clear car markers on seek
        if (state.carMarkersGroup) {
            state.carMarkersGroup.innerHTML = '';
            state.carMarkers = {};
        }
        if (state.trackSvg) {
            state.trackSvg.querySelectorAll('[data-sector]').forEach(p => {
                p.classList.remove('sector-yellow', 'sector-double-yellow');
            });
        }
    });

})();
