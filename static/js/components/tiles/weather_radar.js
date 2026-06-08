/**
 * Weather Tile
 *
 * Listens to: weatherData
 * Displays weather info in tile header (2 lines) and wind arrow over track SVG.
 */

(function() {
    const state = {
        weather: {},
        trackSvgLoaded: false,
        // Radar overlay polling state
        year: null,
        eventName: null,
        sessionType: null,
        radarTimer: null,
        radarObjectUrls: {},   // layer name → blob URL currently in use
        // Geographic alignment state — set once SVG + extent are both known.
        svgWidthM: null,
        svgHeightM: null,
        svgViewBoxAspect: null,  // viewBox.width / viewBox.height
        tileWidthM: null,
        extentFetched: false,
        // Weather-condition icon state
        condLat: null,
        condLng: null,
        conditionDate: null,
        conditionHourly: null,
    };

    const RADAR_POLL_INTERVAL_MS = 30000;
    const RADAR_LAYERS = ["precipitationIntensity"];

    function handleWeather(data) {
        if (!data || typeof data !== 'object') return;
        Object.assign(state.weather, data);
        updateDisplay();
    }

    function updateDisplay() {
        const w = state.weather;

        // Header data items
        const set = (id, val, suffix) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val != null ? `${val}${suffix || ''}` : '--';
        };

        set('liveAirTemp', w.AirTemp, '\u00b0');
        set('liveTrackTemp', w.TrackTemp, '\u00b0');
        set('liveHumidity', w.Humidity, '%');
        set('livePressure', w.Pressure, '');
        set('liveWind', w.WindSpeed ? `${w.WindSpeed} kph` : null);

        // Wind arrow
        updateWindArrow(w.WindDirection, w.WindSpeed);

        // Rainfall indicator
        const rainfall = w.Rainfall === '1' || w.Rainfall === true || w.Rainfall === 1;
        const container = document.getElementById('weatherRadarMap');
        if (container) {
            container.classList.toggle('raining', rainfall);
        }
        // Track rainfall transitions so the radar poll can throttle when
        // it's been dry for a while (= save tomorrow.io quota when
        // refreshes won't change the picture). See fetchAndShowRadar.
        if (rainfall) {
            state.lastRainfallTs = Date.now();
        }
        state.rainfall = rainfall;
    }

    function updateWindArrow(direction, speed) {
        // Mount the arrow inline IMMEDIATELY after the wind-speed
        // value (#liveWind), inside the same .weather-live-item span.
        // No circle border \u2014 just the arrow glyph, sized to match the
        // surrounding text so it reads as a continuation of the value.
        const windSpan = document.getElementById('liveWind');
        if (!windSpan) return;
        const item = windSpan.parentElement;
        if (!item) return;

        let arrow = item.querySelector('.wind-arrow');
        if (!arrow) {
            arrow = document.createElement('span');
            arrow.className = 'wind-arrow';
            // Narrow filled triangle (base = \u00bd width) so direction reads
            // clearly. Points up by default; viewBox centred on (12, 12)
            // so we can rotate-in-place. CSS sizes it at 1em.
            arrow.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 2 L17 22 L7 22 Z" fill="currentColor" class="wind-pointer"/></svg>`;
            // Insert directly AFTER the wind speed text node.
            windSpan.insertAdjacentElement('afterend', arrow);
        }

        if (direction != null) {
            const pointer = arrow.querySelector('.wind-pointer');
            if (pointer) {
                pointer.setAttribute('transform', `rotate(${direction}, 12, 12)`);
            }
            arrow.style.display = '';
            arrow.title = `Wind: ${speed || '?'} kph from ${direction}\u00b0`;
        } else {
            arrow.style.display = 'none';
        }
    }

    // Read viewBox + data-scale from the MAIN track-map SVG to align
    // the Tomorrow.io tile at the same metres-per-pixel scale. Wired
    // into the MutationObserver further down — fires once when the
    // track-map SVG mounts.
    function extractMainTrackDimensions(svgEl) {
        if (state.trackSvgLoaded || !svgEl) return;
        const vb = (svgEl.getAttribute('viewBox') || '').split(/\s+/).map(Number);
        const dataScale = parseFloat(
            svgEl.querySelector('#track-root')?.dataset?.scale || ''
        );
        if (vb.length !== 4 || !(dataScale > 0)) return;
        // SVG raw coords are 0.1 m units; data-scale = viewBox-units
        // per raw-unit, so width-in-metres = viewBox_w / data_scale / 10.
        state.svgWidthM = vb[2] / dataScale / 10;
        state.svgHeightM = vb[3] / dataScale / 10;
        state.svgViewBoxAspect = vb[2] / vb[3];
        state.trackSvgLoaded = true;
        maybeApplyAlignment();
    }

    async function fetchRadarExtent() {
        if (state.extentFetched || !state.eventName) return;
        state.extentFetched = true;
        try {
            const url = `/api/v1/weather/radar/extent?event_name=${encodeURIComponent(state.eventName)}`;
            const resp = await fetch(url);
            if (!resp.ok) return;
            const data = await resp.json();
            state.tileWidthM = data.tile_width_m;
            state.condLat = data.lat;
            state.condLng = data.lng;
            maybeApplyAlignment();
            if (messageBus.clockTime) {
                fetchCondition(messageBus.clockTime.toISOString().slice(0, 10));
                updateConditionIcon();
            }
        } catch (e) {
            // Silently fail; SVG stays at default size.
        }
    }

    function maybeApplyAlignment() {
        if (!state.svgWidthM || !state.tileWidthM || !state.svgViewBoxAspect) return;
        const container = document.getElementById('weatherRadarMap');
        if (!container) return;
        // Use the MAIN track SVG (in #trackMap) for pixel dimensions —
        // the weather tile is now a full-cover overlay over that map,
        // so its rendered metres-per-pixel matches the main SVG's, not
        // the (now-removed) mini-SVG that used to live inside the tile.
        const trackMap = document.getElementById('trackMap');
        const svg = trackMap && trackMap.querySelector('svg');
        if (!svg) return;
        const elW = svg.clientWidth, elH = svg.clientHeight;
        if (elW < 4 || elH < 4) {
            requestAnimationFrame(maybeApplyAlignment);
            return;
        }
        // The SVG element preserves its viewBox aspect (preserveAspectRatio
        // = "meet" by default), so the actual content is letterboxed
        // inside the element box. Compute the real pixel width of the
        // rendered viewBox content:
        const elAspect = elW / elH;
        let contentPxW;
        if (elAspect > state.svgViewBoxAspect) {
            // Element is wider than content → height-bound; horizontal padding
            contentPxW = elH * state.svgViewBoxAspect;
        } else {
            // Element is taller than content → width-bound; vertical padding
            contentPxW = elW;
        }
        // Pixels per metre at which the track is actually rendered.
        const ppm = contentPxW / state.svgWidthM;
        // Match the tile's pixel size to the same scale.
        const tilePx = state.tileWidthM * ppm;
        container.style.setProperty('--tile-size-px', `${tilePx.toFixed(1)}px`);
    }

    // Re-apply alignment on viewport resize so the tile keeps the
    // correct metres-per-pixel scale as the weather container reflows.
    let resizeRafId = null;
    window.addEventListener('resize', () => {
        if (resizeRafId) return;
        resizeRafId = requestAnimationFrame(() => {
            resizeRafId = null;
            maybeApplyAlignment();
        });
    });

    // ── Weather-condition icon ───────────────────────────────────────
    // The radar overlay shows rain only; the overall sky condition
    // (clear, cloud, rain, night) is shown as an icon top-left of the
    // map. Hourly conditions for the circuit come from /api/v1/weather
    // (Open-Meteo) and are indexed by the playback clock, so the icon is
    // correct on replays as well as live.

    const CONDITION_ICONS = {
        'sun': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2 12h2M20 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/></svg>',
        'moon': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
        'cloud': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>',
        'partly-day': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.3"/><path d="M6 1.8v1.5M6 8.7v1.5M1.8 6h1.5M8.7 6h1.5M3 3l1 1M9 3 8 4"/><path vector-effect="non-scaling-stroke" transform="translate(6.5 8) scale(0.6)" d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>',
        'partly-night': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path vector-effect="non-scaling-stroke" transform="scale(0.5)" d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/><path vector-effect="non-scaling-stroke" transform="translate(6.5 8) scale(0.6)" d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>',
        'rain': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="16" y1="13" x2="16" y2="21"/><line x1="8" y1="13" x2="8" y2="21"/><line x1="12" y1="15" x2="12" y2="23"/><path d="M20 16.58A5 5 0 0 0 18 7h-1.26A8 8 0 1 0 4 15.25"/></svg>',
        'thunder': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 16.9A5 5 0 0 0 18 7h-1.26a8 8 0 1 0-11.62 9"/><polyline points="13 11 9 17 15 17 11 23"/></svg>',
        'snow': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 17.58A5 5 0 0 0 18 8h-1.26A8 8 0 1 0 4 16.25"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="8" y1="20" x2="8.01" y2="20"/><line x1="12" y1="18" x2="12.01" y2="18"/><line x1="12" y1="22" x2="12.01" y2="22"/><line x1="16" y1="16" x2="16.01" y2="16"/><line x1="16" y1="20" x2="16.01" y2="20"/></svg>',
    };

    // WMO weather code → [icon key, label]. Open-Meteo `is_day` is 1/0.
    function conditionFor(code, isDay) {
        const day = isDay !== 0;
        if (code <= 1) return day ? ['sun', 'Clear'] : ['moon', 'Clear'];
        if (code === 2) return day ? ['partly-day', 'Partly cloudy']
                                   : ['partly-night', 'Partly cloudy'];
        if (code === 45 || code === 48) return ['cloud', 'Fog'];
        if (code === 3) return ['cloud', 'Overcast'];
        if (code >= 95) return ['thunder', 'Thunderstorm'];
        if ((code >= 71 && code <= 77) || code === 85 || code === 86)
            return ['snow', 'Snow'];
        return ['rain', 'Rain'];
    }

    // Fetch hourly conditions for the circuit on `date` (YYYY-MM-DD, UTC).
    // No-op once already fetched for that date.
    async function fetchCondition(date) {
        if (state.condLat == null || !date || state.conditionDate === date) return;
        state.conditionDate = date;
        try {
            const url = `/api/v1/weather?latitude=${state.condLat}` +
                        `&longitude=${state.condLng}&date=${date}`;
            const resp = await fetch(url);
            if (!resp.ok) { state.conditionDate = null; return; }
            const data = await resp.json();
            state.conditionHourly = data.hourly;
            updateConditionIcon();
        } catch (e) {
            state.conditionDate = null;
        }
    }

    function updateConditionIcon() {
        const h = state.conditionHourly;
        const clock = messageBus.clockTime;
        if (!h || !clock) return;
        const hourKey = clock.toISOString().slice(0, 13);
        const idx = h.time.findIndex(t => t.slice(0, 13) === hourKey);
        if (idx < 0) return;
        renderConditionIcon(h.weather_code[idx], h.is_day[idx]);
    }

    function renderConditionIcon(code, isDay) {
        if (code == null) return;
        // Prefer the overlay title slot (#weatherCondition); fall back
        // to the map container for backward compatibility.
        let el = document.getElementById('weatherCondition');
        if (!el) {
            const container = document.getElementById('weatherRadarMap');
            if (!container) return;
            el = container.querySelector('.weather-condition');
            if (!el) {
                el = document.createElement('div');
                el.className = 'weather-condition';
                container.appendChild(el);
            }
        }
        const [key, label] = conditionFor(code, isDay);
        el.innerHTML = CONDITION_ICONS[key];
        el.title = label;
    }

    // The icon is driven off the playback clock (`clock:update` fires
    // every tick), so it appears as soon as the clock and condition data
    // are both ready and tracks hour boundaries on replay/seek.
    messageBus.on('clock:update', (data) => {
        if (!data || !data.time) return;
        fetchCondition(data.time.toISOString().slice(0, 10));
        updateConditionIcon();
    });

    messageBus.on('weatherData', handleWeather);

    // ── Radar tile polling ───────────────────────────────────────────
    // We learn the session identifiers from sessionInfo + the URL; once
    // all three are known, poll the radar endpoint every 30 s and
    // overlay the returned PNG behind the track SVG.

    messageBus.on('sessionInfo', (data) => {
        if (!data || typeof data !== 'object') return;
        if (data.meetingName) state.eventName = data.meetingName;
        if (data.sessionName) state.sessionType = data.sessionName;
        if (state.year == null) {
            const params = new URLSearchParams(window.location.search);
            const sn = params.get('session') || '';
            const m = sn.match(/^(\d{4})_/);
            if (m) state.year = parseInt(m[1], 10);
        }
        fetchRadarExtent();
        maybeStartRadarPolling();
    });

    function maybeStartRadarPolling() {
        if (state.radarTimer) return;
        if (!state.year || !state.eventName || !state.sessionType) return;
        fetchAndShowRadar();
        state.radarTimer = setInterval(fetchAndShowRadar, RADAR_POLL_INTERVAL_MS);
    }

    async function fetchAndShowRadar() {
        // Throttle when it's been dry for a while: after 10 min of no
        // local rainfall AND no rainfall ever observed this session,
        // skip the fetch entirely — the picture won't change. Keep one
        // refresh per minute as a safety net so we catch incoming rain.
        const DRY_SKIP_MS = 10 * 60 * 1000;
        const now = Date.now();
        const wasEverRaining = state.lastRainfallTs != null;
        const longDry = wasEverRaining
            ? (now - state.lastRainfallTs) > DRY_SKIP_MS
            : true;
        if (!state.rainfall && longDry) {
            // Decimate fetches: only every Nth poll. Counter survives
            // because closure state persists.
            state.drySkipCount = (state.drySkipCount || 0) + 1;
            if (state.drySkipCount % 4 !== 0) return;   // 30s × 4 = 2 min
        } else {
            state.drySkipCount = 0;
        }
        await Promise.all(RADAR_LAYERS.map(fetchLayer));
    }

    async function fetchLayer(layer) {
        // Replays: ask for the tile closest to the current playback
        // clock so the rain pattern matches what was actually overhead
        // at that moment. Live: omit `t` and the server returns the
        // most recent cached tile (= what's overhead now).
        let tParam = '';
        if (messageBus.clockTime) {
            tParam = `&t=${messageBus.clockTime.getTime()}`;
        }
        const url = `/api/v1/weather/radar/latest?year=${state.year}` +
                    `&event_name=${encodeURIComponent(state.eventName)}` +
                    `&session_type=${encodeURIComponent(state.sessionType)}` +
                    `&layer=${encodeURIComponent(layer)}` +
                    tParam +
                    `&_=${Date.now()}`;
        try {
            const resp = await fetch(url);
            if (resp.status !== 200) return;
            const blob = await resp.blob();
            showRadarLayer(layer, URL.createObjectURL(blob));
        } catch (e) {
            // Network blip — swallow; next tick will retry.
        }
    }

    // Replays: refresh the radar tile immediately on a seek so we don't
    // wait up to RADAR_POLL_INTERVAL_MS to show the right rain.
    messageBus.on('state:seek-complete', () => {
        if (state.year && state.eventName && state.sessionType) {
            fetchAndShowRadar();
        }
    });

    function showRadarLayer(layer, objectUrl) {
        const container = document.getElementById('weatherRadarMap');
        if (!container) {
            URL.revokeObjectURL(objectUrl);
            return;
        }
        let img = container.querySelector(`.weather-radar-tile[data-layer="${layer}"]`);
        if (!img) {
            img = document.createElement('img');
            img.className = `weather-radar-tile weather-radar-${layer}`;
            img.dataset.layer = layer;
            // Insert at the very back of the stack so the track SVG
            // and the wind arrow remain on top. CSS controls z-index
            // between the two layers.
            container.insertBefore(img, container.firstChild);
        }
        const previous = state.radarObjectUrls[layer];
        state.radarObjectUrls[layer] = objectUrl;
        img.src = objectUrl;
        if (previous && previous !== objectUrl) {
            URL.revokeObjectURL(previous);
        }
    }

    messageBus.on('state:reset', () => {
        state.weather = {};
        state.conditionDate = null;
        state.conditionHourly = null;
        const cond = document.querySelector('#weatherRadarMap .weather-condition');
        if (cond) cond.remove();
    });

    // =========================================================================
    // Overlay placement — pick a corner / edge that doesn't overlap the
    // track SVG. Run once the track is rendered, and re-run on resize.
    // =========================================================================

    const ANCHOR_CLASSES = [
        'anchor-top-left', 'anchor-top-right',
        'anchor-bottom-left', 'anchor-bottom-right',
        'anchor-left-mid', 'anchor-right-mid',
        'anchor-top-mid', 'anchor-bottom-mid',
    ];

    function applyAnchor(overlay, anchor) {
        for (const cls of ANCHOR_CLASSES) overlay.classList.remove(cls);
        overlay.classList.add(anchor);
    }

    // Sample points along every track path and count how many fall
    // inside the candidate overlay rect. Lower score = less overlap.
    function overlapScore(trackSvg, overlayRect) {
        if (!trackSvg) return 0;
        const paths = trackSvg.querySelectorAll('#track-sectors .track, #track-outline');
        if (!paths.length) return 0;
        let hits = 0;
        for (const p of paths) {
            const len = p.getTotalLength ? p.getTotalLength() : 0;
            if (!len) continue;
            const samples = 60;  // 60 samples per path → cheap, accurate enough
            for (let i = 0; i <= samples; i++) {
                const pt = p.getPointAtLength((len * i) / samples);
                const screenPt = pt.matrixTransform(p.getScreenCTM());
                if (screenPt.x >= overlayRect.left && screenPt.x <= overlayRect.right
                        && screenPt.y >= overlayRect.top && screenPt.y <= overlayRect.bottom) {
                    hits++;
                }
            }
        }
        return hits;
    }

    function rectForAnchor(anchor, container, overlaySize) {
        const c = container.getBoundingClientRect();
        const w = overlaySize.width, h = overlaySize.height;
        const pad = 6;
        let left, top;
        switch (anchor) {
            case 'anchor-top-left':     left = c.left + pad;            top = c.top + pad;            break;
            case 'anchor-top-right':    left = c.right - w - pad;       top = c.top + pad;            break;
            case 'anchor-bottom-left':  left = c.left + pad;            top = c.bottom - h - pad;     break;
            case 'anchor-bottom-right': left = c.right - w - pad;       top = c.bottom - h - pad;     break;
            case 'anchor-left-mid':     left = c.left + pad;            top = c.top + (c.height - h) / 2; break;
            case 'anchor-right-mid':    left = c.right - w - pad;       top = c.top + (c.height - h) / 2; break;
            case 'anchor-top-mid':      left = c.left + (c.width - w) / 2; top = c.top + pad;          break;
            case 'anchor-bottom-mid':   left = c.left + (c.width - w) / 2; top = c.bottom - h - pad;   break;
            default: return null;
        }
        return { left, top, right: left + w, bottom: top + h };
    }

    function positionWeatherOverlay() {
        const overlay = document.getElementById('weatherOverlay');
        const trackContainer = document.getElementById('trackMap');
        if (!overlay || !trackContainer) return;
        const trackSvg = trackContainer.querySelector('svg');
        if (!trackSvg) return;  // track not loaded yet
        const overlayRect0 = overlay.getBoundingClientRect();
        if (!overlayRect0.width || !overlayRect0.height) return;
        const size = { width: overlayRect0.width, height: overlayRect0.height };
        let best = null;
        let bestScore = Infinity;
        for (const anchor of ANCHOR_CLASSES) {
            const r = rectForAnchor(anchor, trackContainer, size);
            if (!r) continue;
            const score = overlapScore(trackSvg, r);
            if (score === 0) { best = anchor; bestScore = 0; break; }
            if (score < bestScore) { best = anchor; bestScore = score; }
        }
        if (best) applyAnchor(overlay, best);
    }

    // Watch for the track SVG to be appended (track_map.js sets
    // innerHTML on #trackMap after fetching the circuit SVG).
    const trackMapEl = document.getElementById('trackMap');
    if (trackMapEl && typeof MutationObserver !== 'undefined') {
        const mo = new MutationObserver(() => {
            const svg = trackMapEl.querySelector('svg');
            if (svg) {
                // Defer one tick so the SVG has its final layout.
                requestAnimationFrame(() => {
                    // Pull the metres-per-pixel scale from the main
                    // track SVG so the weather radar tile aligns at the
                    // same scale (replaces the old mini-SVG loader).
                    extractMainTrackDimensions(svg);
                    positionWeatherOverlay();
                });
            }
        });
        mo.observe(trackMapEl, { childList: true, subtree: true });
    }
    // Re-position on viewport resize (tile width can change with the layout).
    window.addEventListener('resize', () => {
        requestAnimationFrame(positionWeatherOverlay);
    });

})();
