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
        markerOverlay: null,   // transparent overlay SVG holding the markers (card z9L5gqpj)
        overlayRO: null,       // ResizeObserver keeping the overlay box on the track SVG's rect
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

                buildMarkerOverlay(trackMap, svgElement);   // markers live on their own layer

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

    // Markers used to live inside the track SVG (#car-markers), so moving one each frame repainted
    // that SVG — including the dense rain-radar contours it overlaps. Put them on a SEPARATE
    // transparent overlay SVG instead: it shares the track's viewBox + track-root (F1→user) matrix,
    // so markers land at identical pixels, but moving them only repaints the overlay. (card z9L5gqpj)
    function buildMarkerOverlay(container, trackSvg) {
        const vb = trackSvg.getAttribute('viewBox') || '0 0 100 100';
        const overlay = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        overlay.setAttribute('viewBox', vb);
        overlay.setAttribute('class', 'track-marker-layer');
        const root = trackSvg.querySelector('#track-root');
        const m = (root && root.transform.baseVal.numberOfItems)
            ? root.transform.baseVal.consolidate().matrix : null;
        const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        group.setAttribute('id', 'car-markers-overlay');
        if (m) group.setAttribute('transform',
            `matrix(${m.a} ${m.b} ${m.c} ${m.d} ${m.e} ${m.f})`);
        overlay.appendChild(group);
        container.appendChild(overlay);
        state.markerOverlay = overlay;
        state.carMarkers = {};        // markers are re-created into the fresh group on next frame
        state.carMarkersGroup = group;
        // Click a car → focus that driver (+ neighbours in a race) on the dashboard.
        group.addEventListener('click', (e) => {
            const mk = e.target.closest('.car-marker');
            if (mk && window.F1Dashboard) window.F1Dashboard.focus(mk.dataset.driver);
        });
        syncMarkerOverlayBox();
        if (state.overlayRO) state.overlayRO.disconnect();
        state.overlayRO = new ResizeObserver(syncMarkerOverlayBox);
        state.overlayRO.observe(container);
    }

    // Keep the overlay box exactly on the (aspect-fitted, centred) track SVG's rendered rect, so
    // the two share a pixel mapping. Re-run whenever the tile resizes.
    function syncMarkerOverlayBox() {
        const ov = state.markerOverlay, trk = state.trackSvg;
        const cont = document.getElementById('trackMap');
        if (!ov || !trk || !cont) return;
        const cr = cont.getBoundingClientRect(), tr = trk.getBoundingClientRect();
        ov.style.setProperty('--ov-left', (tr.left - cr.left).toFixed(1) + 'px');
        ov.style.setProperty('--ov-top', (tr.top - cr.top).toFixed(1) + 'px');
        ov.style.setProperty('--ov-w', tr.width.toFixed(1) + 'px');
        ov.style.setProperty('--ov-h', tr.height.toFixed(1) + 'px');
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
    const MINI_ZOOM = 10;              // pan zoom (px-per-user = MINI_ZOOM × meet-fit scale)
    const MINI_MARKER_SCALE = 0.15;    // marker size (F1 radius) — small so the GPS trail shows the racing line
    const MINI_SMOOTH = 0.18;          // pan-centre low-pass (0..1) — damps GPS jitter at high zoom
    // The mini-map follows the chaser by TRANSLATING one GPU-composited wrapper (.mini-pan)
    // that carries two layers, rather than reframing the SVG viewBox each frame (which forced a
    // full re-raster of the track strokes at high zoom, 60×/s). (card pCsVZRlp)
    //   trackSvg   — track paths + flag colouring; rasterised once, only ever panned.
    //   markerSvg  — transparent overlay sharing the same viewBox + F1→user matrix; only this
    //                layer repaints when cars move (a few dots), so per-frame cost is tiny.
    const _mini = { container: null, pan: null, trackSvg: null, markerSvg: null, markerGroup: null,
                    markers: {}, focus: null, vb: null, matrix: null, s: 0, cw: 0, ch: 0,
                    pending: null, sm: {}, warnEl: null, ro: null };
    // Latest position/telemetry-outage warning (mirrors the main map's #trackPosWarning).
    let _posWarn = { active: false, red: false, msg: '' };

    const SVGNS = 'http://www.w3.org/2000/svg';

    function mountMini(container) {
        if (!container) return;
        if (!state.trackSvg) { _mini.pending = container; return; }   // SVG not loaded yet → retry on load
        _mini.pending = null;
        const vb = (state.trackSvg.getAttribute('viewBox') || '0 0 100 100').split(/\s+/).map(Number);

        // Track layer — clone of the main map, paths + flag colouring only (markers stripped).
        const trackSvg = state.trackSvg.cloneNode(true);
        trackSvg.classList.add('mini-map', 'mini-track-layer');   // .mini-map scopes track styling
        const oldMarkers = trackSvg.querySelector('#car-markers');
        if (oldMarkers) oldMarkers.remove();   // markers live on the overlay layer now
        // The clone inherits the main map's F1-unit stroke-widths, which render far heavier at the
        // mini zoom. Set the centre line explicitly. (state.markerStrokeWidth === f1PerPx.)
        trackSvg.querySelectorAll('.track').forEach(el =>
            el.setAttribute('stroke-width', (5 * state.markerStrokeWidth).toFixed(1)));

        // Constant F1-coords → SVG-user-space matrix (track-root transform); the original markers
        // were children of track-root, so this maps a car's (x,y) into the viewBox coordinate system.
        const root = trackSvg.querySelector('#track-root');
        const matrix = (root && root.transform.baseVal.numberOfItems)
            ? root.transform.baseVal.consolidate().matrix : null;

        // Marker overlay — transparent, same viewBox + same F1→user matrix as the track layer, so
        // markers land at identical pixels. Redrawing markers here never touches the track layer.
        const markerSvg = document.createElementNS(SVGNS, 'svg');
        markerSvg.setAttribute('viewBox', vb.join(' '));
        markerSvg.setAttribute('class', 'mini-marker-layer');
        const markerGroup = document.createElementNS(SVGNS, 'g');
        if (matrix) markerGroup.setAttribute('transform',
            `matrix(${matrix.a} ${matrix.b} ${matrix.c} ${matrix.d} ${matrix.e} ${matrix.f})`);
        markerSvg.appendChild(markerGroup);

        // Both layers ride on one wrapper — the single GPU-composited element we translate.
        const pan = document.createElement('div');
        pan.className = 'mini-pan';
        pan.appendChild(trackSvg);
        pan.appendChild(markerSvg);
        container.innerHTML = ''; container.appendChild(pan);

        _mini.container = container; _mini.pan = pan; _mini.trackSvg = trackSvg;
        _mini.markerSvg = markerSvg; _mini.markerGroup = markerGroup; _mini.markers = {};
        _mini.vb = vb; _mini.matrix = matrix;

        layoutMini();                                  // size the pan to the zoomed track extent
        if (_mini.ro) _mini.ro.disconnect();
        _mini.ro = new ResizeObserver(layoutMini);     // re-derive px scale when the cell resizes
        _mini.ro.observe(container);
        applyMiniWarning();   // reflect any active outage immediately (e.g. mounted mid-outage)
    }

    // Derive the px-per-user scale from the current cell size and size the pan wrapper to the
    // full zoomed track (so its raster is crisp; renderMini only ever translates it after).
    function layoutMini() {
        if (!_mini.pan || !_mini.vb) return;
        const cw = _mini.container.clientWidth, ch = _mini.container.clientHeight;
        if (!cw || !ch) return;
        const vw = _mini.vb[2], vh = _mini.vb[3];
        const s = MINI_ZOOM * Math.min(cw / vw, ch / vh);   // mirrors the old viewBox 'meet' crop
        _mini.cw = cw; _mini.ch = ch; _mini.s = s;
        _mini.pan.style.setProperty('--mini-w', (vw * s).toFixed(1) + 'px');
        _mini.pan.style.setProperty('--mini-h', (vh * s).toFixed(1) + 'px');
    }
    // When a position/telemetry outage is active, hide the mini-map and show the SAME warning
    // message the main track map shows (server dataHealth → updatePosWarning).
    function applyMiniWarning() {
        if (!_mini.pan || !_mini.container) return;
        if (!_mini.warnEl) {
            const w = document.createElement('div');
            w.className = 'pos-warning mini-pos-warning';
            w.hidden = true;
            w.innerHTML =
                `<svg class="pos-warning-icon" viewBox="0 0 24 24" aria-hidden="true">
                    <path class="pos-warning-tri" d="M12 3 L22.5 21 L1.5 21 Z"/>
                    <rect class="pos-warning-excl" x="11" y="9" width="2" height="6" rx="1"/>
                    <circle class="pos-warning-excl" cx="12" cy="18" r="1.2"/>
                </svg><span class="pos-warning-msg"></span>`;
            _mini.container.appendChild(w);
            _mini.warnEl = w;
        }
        _mini.warnEl.hidden = !_posWarn.active;
        _mini.warnEl.classList.toggle('red', _posWarn.red);
        _mini.warnEl.querySelector('.pos-warning-msg').textContent = _posWarn.msg;
        _mini.pan.style.display = _posWarn.active ? 'none' : '';
    }
    function unmountMini() {
        if (_mini.ro) { _mini.ro.disconnect(); _mini.ro = null; }
        if (_mini.container) _mini.container.innerHTML = '';
        _mini.container = _mini.pan = _mini.trackSvg = _mini.markerSvg = _mini.markerGroup =
            _mini.matrix = _mini.warnEl = null;
        _mini.vb = null; _mini.markers = {}; _mini.sm = {};
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
        if (!m && _mini.markerGroup) {
            m = createMiniMarker(info.color || DEFAULT_CAR_COLOR, info.tla || num);
            _mini.markers[num] = m; _mini.markerGroup.appendChild(m);
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
        if (!_mini.pan || !_mini.markerGroup) return;
        for (const num in posBuf) {
            const buf = posBuf[num]; if (!buf.length) continue;
            const st = state.driverStatus[num];
            if (st === 'RET' || st === 'STOP') { removeMiniMarker(num); delete _mini.sm[num]; continue; }
            const s = miniSmoothed(num, t);
            updateMiniMarker(num, s.x, s.y);
        }
        for (const num in _mini.markers) if (!posBuf[num]) { removeMiniMarker(num); delete _mini.sm[num]; }
        // Follow the chaser: pan both layers so the focus car's SAME smoothed point lands dead-centre
        // (marker + frame read the identical value → zero swim). This is a CSS translate on the
        // wrapper — composited, no re-raster — unlike the old per-frame viewBox reframe.
        const fp = _mini.focus && _mini.sm[_mini.focus];
        if (fp && _mini.matrix && _mini.s) {
            const M = _mini.matrix;
            const ux = M.a * fp.x + M.c * fp.y + M.e;   // focus car in viewBox user-space
            const uy = M.b * fp.x + M.d * fp.y + M.f;
            const tx = _mini.cw / 2 - (ux - _mini.vb[0]) * _mini.s;   // px shift to centre it
            const ty = _mini.ch / 2 - (uy - _mini.vb[1]) * _mini.s;
            _mini.pan.style.setProperty('--mtx', tx.toFixed(2) + 'px');
            _mini.pan.style.setProperty('--mty', ty.toFixed(2) + 'px');
        }
        syncMiniFlags();
    }

    // Mirror the main map's track-status flash + yellow-flag sectors onto the clone.
    function syncMiniFlags() {
        if (!_mini.trackSvg || !state.trackSvg) return;
        ['#track-outline', '#track-sectors'].forEach(sel => {
            const a = state.trackSvg.querySelector(sel), b = _mini.trackSvg.querySelector(sel);
            if (!a || !b) return;
            b.classList.toggle('flag-blink', a.classList.contains('flag-blink'));
            const col = a.style.getPropertyValue('--flag-color');
            if (col) b.style.setProperty('--flag-color', col);
        });
        const yellow = new Set();
        state.trackSvg.querySelectorAll('[data-sector].sector-yellow')
            .forEach(e => yellow.add(e.getAttribute('data-sector')));
        _mini.trackSvg.querySelectorAll('[data-sector]').forEach(p => {
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
            _posWarn = { active: true, red: true, msg: msg.textContent };
        } else if (posRed || telRed) {
            el.classList.remove('red');
            msg.textContent = 'Position data unavailable. Track position estimated from telemetry';
            el.hidden = false;
            _posWarn = { active: true, red: false, msg: msg.textContent };
        } else {
            el.hidden = true;
            _posWarn = { active: false, red: false, msg: '' };
        }
        applyMiniWarning();   // hide the mini-map + show the same message during an outage
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
