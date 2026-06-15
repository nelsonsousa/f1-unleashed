const API_BASE = '/api/v1';

// Open the timing client in the SAME tab so live and replay flows feel
// identical and browser history works (the timing page has a Close (X)
// button in the header to go back).
function openTimingWindow(url, sessionKey) {
    window.location.href = url;
}

function bindTimingWindow(elementId, url, sessionKey) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.href = url;
    el.onclick = (e) => {
        e.preventDefault();
        openTimingWindow(url, sessionKey);
    };
}

// Auth state
let authState = {
    isAuthenticated: false,
    subscriptionStatus: null,
    expiresAt: null,
};

// Countdown state
let nextSession = null;
let countdownInterval = null;
let liveCheckInterval = null;

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    await checkAuthStatus();
    await checkForLiveSession();
    await setupYearDropdown();
    await refreshCache();
    loadVersionInfo();   // non-blocking — version is informational
});

// Version + update indicator (footer). Notification only — no auto-update.
async function loadVersionInfo() {
    try {
        const data = await fetchJSON(`${API_BASE}/version`);
        const vEl = document.getElementById('appVersion');
        if (vEl && data.version) vEl.textContent = `v${data.version}`;
        if (data.update_available) {
            const up = document.getElementById('updateAvailable');
            if (up) {
                up.textContent = `· Update available${data.latest ? ` (${data.latest})` : ''}`;
                if (data.release_url) up.href = data.release_url;
                up.classList.remove('hidden');
            }
        }
    } catch (e) { /* version is non-critical — ignore */ }
}

// Open the cache root folder in the OS file explorer.
window.openCacheFolder = async function() {
    try {
        const resp = await fetch(`${API_BASE}/livetiming/open-cache-folder`, { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    } catch (e) {
        console.error(e);
        alert('Could not open the cache folder.');
    }
};

// =========================================================================
// Auth Functions
// =========================================================================

async function checkAuthStatus() {
    try {
        const data = await fetchJSON(`${API_BASE}/auth/status`);
        authState.isAuthenticated = data.is_authenticated;
        authState.subscriptionStatus = data.subscription_status;
        authState.expiresAt = data.expires_at;
        updateAuthUI(data);
    } catch (error) {
        console.error('Error checking auth status:', error);
        updateAuthUI({ is_authenticated: false, error: 'Failed to check status' });
    }
}

function updateAuthUI(status) {
    const authStatus = document.getElementById('authStatus');
    const loginBtn = document.getElementById('loginBtn');
    const indicator = authStatus.querySelector('.auth-indicator');
    const text = authStatus.querySelector('.auth-text');

    if (status.is_authenticated) {
        indicator.className = 'auth-indicator logged-in';
        text.innerHTML = `Logged in${status.subscribed_product ? ` <span style="color: var(--green);">(${status.subscribed_product})</span>` : ''}`;
        loginBtn.textContent = 'Logout';
        loginBtn.className = 'btn-login logged-in';
    } else {
        indicator.className = 'auth-indicator not-logged-in';
        text.textContent = status.error || 'Not logged in';
        loginBtn.textContent = 'Login to F1';
        loginBtn.className = 'btn-login';
    }
}

async function handleLogin() {
    if (authState.isAuthenticated) {
        if (!confirm('Are you sure you want to logout?')) return;

        try {
            await fetch(`${API_BASE}/auth/logout`, { method: 'POST' });
            await checkAuthStatus();
        } catch (error) {
            console.error('Error logging out:', error);
        }
    } else {
        try {
            const response = await fetch(`${API_BASE}/auth/browser-login`, { method: 'POST' });
            const data = await response.json();

            if (data.success) {
                showLoginInstructions();
            } else {
                showLoginError(data.error || 'Failed to open login window');
            }
        } catch (error) {
            showLoginError(error.message);
        }
    }
}

function showLoginInstructions() {
    const modal = document.getElementById('loginModal');
    const body = document.getElementById('loginModalBody');

    body.innerHTML = `
        <p>A login window has been opened. Please log in with your F1 account.</p>
        <p>After logging in, the window will show "Login Complete" and close automatically.</p>
        <div class="modal-actions">
            <button onclick="closeLoginModal()" class="btn-secondary">Close</button>
            <button onclick="checkAuthStatus(); closeLoginModal();" class="btn-primary">I've Logged In - Refresh</button>
        </div>
    `;

    modal.classList.add('active');
}

function showLoginError(error) {
    const modal = document.getElementById('loginModal');
    const body = document.getElementById('loginModalBody');

    body.innerHTML = `
        <p style="color: var(--accent);">Login failed: ${error}</p>
        <div class="modal-actions">
            <button onclick="closeLoginModal()" class="btn-secondary">Close</button>
        </div>
    `;

    modal.classList.add('active');
}

function closeLoginModal() {
    document.getElementById('loginModal').classList.remove('active');
}

// Close modal on outside click
document.getElementById('loginModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'loginModal') closeLoginModal();
});

// =========================================================================
// Live Session Detection (F1 livetiming API)
// =========================================================================

async function checkForLiveSession() {
    try {
        const response = await fetch(`${API_BASE}/schedule/live-session`);

        if (response.status === 204) {
            // No live session — only load countdown if we don't already have one
            // (avoids resetting countdown when polling during the transition period)
            if (!nextSession) {
                await loadNextSession();
            }
            return;
        }

        if (!response.ok) {
            // API error — fall back to schedule if no countdown active
            if (!nextSession) {
                await loadNextSession();
            }
            return;
        }

        const data = await response.json();
        showLiveSession(data);

        // Keep polling every 60s to detect when session ends
        startLiveCheck();

    } catch (error) {
        console.error('Error checking live session:', error);
        if (!nextSession) {
            await loadNextSession();
        }
    }
}

function startLiveCheck() {
    if (liveCheckInterval) return;
    liveCheckInterval = setInterval(checkForLiveSession, 30000);
}

function stopLiveCheck() {
    if (liveCheckInterval) {
        clearInterval(liveCheckInterval);
        liveCheckInterval = null;
    }
}

function showLiveSession(data) {
    // Stop countdown if running
    if (countdownInterval) {
        clearInterval(countdownInterval);
        countdownInterval = null;
    }

    document.getElementById('nextEventName').textContent = data.event_name;
    document.getElementById('nextSessionName').textContent = data.session_name;
    document.getElementById('countdownTimer').classList.add('hidden');
    document.getElementById('liveIndicator').classList.remove('hidden');
    document.getElementById('countdownDate').classList.add('hidden');

    const liveBadge = document.querySelector('.live-badge');
    liveBadge.textContent = 'LIVE';
    liveBadge.classList.remove('pre-session');

    document.getElementById('liveMessage').textContent =
        `${data.event_name} - ${data.session_name} is live`;

    // Build session param for links — use cache-compatible format: year_meetingKey_location_sessionType
    const year = new Date().getFullYear();
    const meetingKey = data.meeting_key || 0;
    const location = (data.location || '').replace(/ /g, '_');
    const sessionType = (data.session_name || data.session_type || '').replace(/ /g, '_');
    const sessionParam = encodeURIComponent(`${year}_${meetingKey}_${location}_${sessionType}`);
    const liveUrl = `/${data.page}?session=${sessionParam}&mode=live`;
    const startUrl = `/${data.page}?session=${sessionParam}&mode=start`;
    bindTimingWindow('btnLive', liveUrl, sessionParam);
    bindTimingWindow('btnFromStart', startUrl, sessionParam);

    document.getElementById('countdownSection').classList.add('is-live');
    document.getElementById('countdownSection').classList.remove('past-event');
}

// =========================================================================
// Schedule Countdown (fallback when no live session)
// =========================================================================

async function loadNextSession() {
    try {
        const data = await fetchJSON(`${API_BASE}/schedule/next-session`);
        // 204-style empty / missing core fields = no next session.
        if (!data || !data.event_name || !data.session_date) {
            throw new Error('empty next-session');
        }
        nextSession = data;
        updateCountdownDisplay();
        showCountdown();
        startCountdown();
    } catch (error) {
        console.error('Error loading next session:', error);
        document.getElementById('nextEventName').textContent = 'No upcoming sessions';
        document.getElementById('nextSessionName').textContent = '';
        document.getElementById('countdownTimer').classList.add('hidden');
        document.getElementById('liveIndicator').classList.add('hidden');
    }
}

function showCountdown() {
    document.getElementById('countdownTimer').classList.remove('hidden');
    document.getElementById('liveIndicator').classList.add('hidden');
    document.getElementById('countdownDate').classList.remove('hidden');
    document.getElementById('countdownSection').classList.remove('is-live');
}

function updateCountdownDisplay() {
    if (!nextSession) return;

    document.getElementById('nextEventName').textContent = nextSession.event_name;
    document.getElementById('nextSessionName').textContent = nextSession.session_name;

    const sessionDate = new Date(nextSession.session_date);
    document.getElementById('countdownDate').textContent = sessionDate.toLocaleString('en-US', {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        timeZoneName: 'short'
    });
}

function startCountdown() {
    if (countdownInterval) {
        clearInterval(countdownInterval);
    }

    updateCountdownTimer();
    countdownInterval = setInterval(updateCountdownTimer, 1000);
}

function updateCountdownTimer() {
    if (!nextSession) return;

    const now = new Date();
    const sessionDate = new Date(nextSession.session_date);
    const diff = sessionDate - now;

    if (diff <= 0) {
        // Session start time reached — switch to periodic live polling
        clearInterval(countdownInterval);
        countdownInterval = null;

        // Show "Session starting soon..." state
        document.getElementById('countdownTimer').classList.add('hidden');
        const liveBadge = document.querySelector('.live-badge');
        liveBadge.textContent = 'STARTING';
        liveBadge.classList.add('pre-session');
        document.getElementById('liveIndicator').classList.remove('hidden');
        document.getElementById('liveMessage').textContent =
            `${nextSession.event_name} - ${nextSession.session_name} starting soon...`;

        // Poll every 30s until a live session is found
        checkForLiveSession();
        startLiveCheck();
        return;
    }

    const days = Math.floor(diff / (1000 * 60 * 60 * 24));
    const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((diff % (1000 * 60)) / 1000);

    document.getElementById('days').textContent = days;
    document.getElementById('hours').textContent = hours.toString().padStart(2, '0');
    document.getElementById('minutes').textContent = minutes.toString().padStart(2, '0');
    document.getElementById('seconds').textContent = seconds.toString().padStart(2, '0');
}

// =========================================================================
// Utility
// =========================================================================

async function fetchJSON(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
}

// =========================================================================
// Season Schedule + Cards (with localStorage caching)
// =========================================================================

const SCHEDULE_TTL_MS = 24 * 60 * 60 * 1000;   // refresh schedule once a day
const CACHE_KEY_VERSION = 'v2';                 // bump to invalidate stale entries
let cachedKeysByLocation = {};   // location → Set of normalised session names
let currentYear = null;
let currentEvents = [];

async function setupYearDropdown() {
    const sel = document.getElementById('yearSelect');
    if (!sel) return;
    let years = [];
    try {
        const data = await fetchCached(`${API_BASE}/years`, 'years', SCHEDULE_TTL_MS);
        years = data.years || [];
    } catch (e) {
        const current = new Date().getFullYear();
        years = [current, current - 1, current - 2];
    }
    sel.innerHTML = years
        .map(y => `<option value="${y}">${y}</option>`)
        .join('');
    const calendarYear = String(new Date().getFullYear());
    sel.value = years.includes(parseInt(calendarYear, 10)) ? calendarYear : String(years[0]);
    sel.addEventListener('change', () => loadSchedule(parseInt(sel.value, 10)));
    await loadSchedule(parseInt(sel.value, 10));
}

// Generic localStorage cache for GET endpoints with a TTL. The home
// page schedule doesn't need to round-trip the server on every load.
// Key versioning: bumping CACHE_KEY_VERSION invalidates older entries
// (which had the wrong shape — pre-sessions-extension schedule).
async function fetchCached(url, key, ttlMs) {
    const storeKey = `f1unleashed.cache.${CACHE_KEY_VERSION}.${key}`;
    try {
        const raw = localStorage.getItem(storeKey);
        if (raw) {
            const env = JSON.parse(raw);
            if (env.t && (Date.now() - env.t) < ttlMs) return env.v;
        }
    } catch (e) { /* ignore parse errors */ }
    const v = await fetchJSON(url);
    try { localStorage.setItem(storeKey, JSON.stringify({ t: Date.now(), v })); } catch (e) {}
    return v;
}

// Force-refresh a cached schedule (called by the Refresh button + when
// live session events fire).
async function refreshSchedule(year) {
    try { localStorage.removeItem(`f1unleashed.cache.schedule_${year}`); } catch (e) {}
    await loadSchedule(year);
}

async function loadSchedule(year) {
    const grid = document.getElementById('scheduleGrid');
    if (!grid) return;
    currentYear = year;
    grid.innerHTML = '<div class="loading">Loading…</div>';
    try {
        const data = await fetchCached(
            `${API_BASE}/schedule/${year}`, `schedule_${year}`, SCHEDULE_TTL_MS
        );
        const events = (data.events || []).filter(e =>
            !/test/i.test(e.name || '')   // drop pre-season testing rows
        );
        currentEvents = events;
        await loadCachedKeys();  // ensure cache map is fresh
        renderScheduleCards(year, events);
    } catch (e) {
        grid.innerHTML = `<div class="loading error">Failed to load ${year} schedule.</div>`;
    }
}

// Build a {`${year}|${location}`: {sessionName: cachedEntry}} map.
// Year-scoped so a 2025 Melbourne event doesn't pick up a 2026
// Melbourne cache entry (location alone is ambiguous across seasons).
async function loadCachedKeys() {
    try {
        const sessions = await fetchJSON(`${API_BASE}/livetiming/cached`);
        cachedKeysByLocation = {};
        for (const s of sessions) {
            const key = `${s.year || ''}|${(s.location || '').toLowerCase()}`;
            if (!cachedKeysByLocation[key]) cachedKeysByLocation[key] = {};
            const cleanName = String(s.session || '')
                .replace(/^\d+\s+/, '')   // drop "11270 " prefix
                .toLowerCase();
            cachedKeysByLocation[key][cleanName] = s;
        }
    } catch (e) { /* fall through — no badges */ }
}

function eventCacheKey(ev) {
    return `${currentYear || ''}|${(ev.location || '').toLowerCase()}`;
}
function eventHasAnyCached(ev) {
    const map = cachedKeysByLocation[eventCacheKey(ev)];
    return !!(map && Object.keys(map).length > 0);
}

function sessionCachedEntry(ev, sessionName) {
    const map = cachedKeysByLocation[eventCacheKey(ev)];
    return map ? map[String(sessionName).toLowerCase()] : null;
}

function sessionIsCached(ev, sessionName) {
    return !!sessionCachedEntry(ev, sessionName);
}

function eventAllCached(ev) {
    const ss = ev.sessions || [];
    if (ss.length === 0) return false;
    return ss.every(s => sessionIsCached(ev, s.name));
}

function renderScheduleCards(year, events) {
    const grid = document.getElementById('scheduleGrid');
    if (!grid) return;
    if (events.length === 0) {
        grid.innerHTML = '<div class="loading">No events for this season.</div>';
        return;
    }
    const now = new Date();
    let html = '';
    for (const e of events) {
        const eventDate = new Date(e.date);
        const cached = eventHasAnyCached(e);
        // "past" gates the click handler. An event is clickable when its
        // date has passed OR when ANY session of it is already cached
        // (= weekend has begun even if the headline race date is still
        // in the future, e.g. Monaco Sunday with FP/Q already done).
        const past = eventDate < now || cached;
        const cardCls = [
            'gp-card',
            past ? '' : 'upcoming',
            cached ? 'cached' : '',
        ].filter(Boolean).join(' ');
        const onclick = past ? `onclick="toggleEvent(event, ${e.round})"` : '';
        const badge = cached
            ? '<span class="gp-badge cached" title="Sessions cached locally">✓</span>'
            : (past ? '<span class="gp-badge missing" title="Not cached">·</span>' : '');
        html += `
            <div class="${cardCls}" data-round="${e.round}" ${onclick}>
                <div class="gp-row1">
                    <span class="gp-round">${e.round}</span>
                    ${badge}
                </div>
                <div class="gp-name">${escapeHtml(e.name)}</div>
                <div class="gp-loc">${escapeHtml(e.location)}, ${escapeHtml(e.country)}</div>
                <div class="gp-date">${escapeHtml(eventDateRange(e))}</div>
            </div>
        `;
    }
    grid.innerHTML = html;
}

// Event date range from the first → last session in the sessions list
// (e.g. "Mar 6 – Mar 8" for a Fri-Sun race weekend). Falls back to the
// event-day date when sessions aren't populated.
function eventDateRange(ev) {
    const fmt = d => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const ss = ev.sessions || [];
    const dates = ss.map(s => s.date_utc ? new Date(s.date_utc + 'Z') : null).filter(d => d && !isNaN(d));
    if (dates.length >= 2) {
        const lo = new Date(Math.min(...dates.map(d => d.getTime())));
        const hi = new Date(Math.max(...dates.map(d => d.getTime())));
        const a = fmt(lo), b = fmt(hi);
        return a === b ? a : `${a} – ${b}`;
    }
    return fmt(new Date(ev.date));
}

function renderSessionPopoverHtml(year, ev) {
    const sessions = ev.sessions || [];
    let cards = '';
    for (const s of sessions) {
        const entry = sessionCachedEntry(ev, s.name);
        const cached = !!entry;
        const sd = s.date_utc ? new Date(s.date_utc + 'Z') : null;
        const dlabel = sd ? sd.toLocaleString('en-GB', {
            weekday: 'short', day: 'numeric', month: 'short',
            hour: '2-digit', minute: '2-digit', hour12: false,
        }) : '';
        const sessionType = sessionTypeFromName(s.name);
        // Use the CACHED entry's cache-key (e.g. 2026_1281_Suzuka_11246_Practice_1)
        // which carries meeting + session F1 keys — the server's session
        // lookup expects exactly that shape.
        const openUrl = cached
            ? `/${sessionType}?session=${encodeURIComponent(entry.name)}`
            : '';
        // Button label: "Watch live" if the session is currently
        // running (live capture in progress), "Replay" if cached,
        // "Download" otherwise.
        const isLive = !!(entry && entry.is_live);
        const action = isLive
            ? `<a class="ses-btn live" href="${openUrl}" target="_self">Watch live</a>`
            : cached
                ? `<a class="ses-btn open" href="${openUrl}" target="_self">Replay</a>`
                : `<button class="ses-btn download" onclick="downloadSession(${year}, '${escapeAttr(ev.name)}', '${escapeAttr(s.name)}', this)">Download</button>`;
        cards += `
            <div class="session-card${cached ? ' cached' : ''}">
                <div class="ses-name">${escapeHtml(s.name)}</div>
                <div class="ses-when">${escapeHtml(dlabel)}</div>
                ${renderSessionStatusIcons(entry, year, ev, s)}
                ${action}
            </div>
        `;
    }
    const allCached = eventAllCached(ev);
    const downloadAllBtn = allCached
        ? `<button class="ses-btn download-all done" disabled title="All sessions cached">Download all</button>`
        : `<button class="ses-btn download-all" onclick="downloadAllSessions(${year}, '${escapeAttr(ev.name)}', this)">Download all</button>`;
    cards += `
        <div class="session-card actions">
            ${downloadAllBtn}
        </div>
    `;
    return cards;
}

// Health status → CSS colour class (grey/red/yellow/green).
function statusClass(status) {
    return {
        complete:   'st-ok',
        incomplete: 'st-warn',
        corrupted:  'st-err',
        absent:     'st-absent',
    }[status] || 'st-absent';
}

// Per-session status icons (cards): data + audio stacked, each coloured by its
// 4-state health (grey/red/yellow/green) with the reason in the tooltip, plus a
// weather-presence icon (green/grey). A re-download button sits next to the data
// icon whenever data isn't complete; audio can't be re-downloaded, so it never
// gets one.
function renderSessionStatusIcons(entry, year, ev, s) {
    const dataS  = (entry && entry.data_status)
        || (entry && entry.has_jsonl ? 'complete' : 'absent');
    const audioS = (entry && entry.audio_status)
        || (entry && entry.has_audio ? 'complete' : 'absent');
    const wxS    = (entry && entry.weather_status) || 'absent';
    const dataReason  = (entry && entry.data_reason)  || `Data ${dataS}`;
    const audioReason = (entry && entry.audio_reason) || `Audio ${audioS}`;
    const wxReason    = wxS === 'complete' ? 'Weather data cached' : 'No weather data';

    const fileSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
    const audioSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M3 9v6h4l5 5V4L7 9H3z"/><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>`;
    const wxSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 6.5 19z"/></svg>`;

    const dlBtn = (dataS !== 'complete')
        ? `<button class="ses-redl" title="${entry ? 'Re-download timing data' : 'Download timing data'}" onclick="redownloadSession(${year}, '${escapeAttr(ev.name)}', '${escapeAttr(s.name)}', this)">&#8595;</button>`
        : '';

    return `
        <div class="ses-icons">
            <div class="ses-ico-row">
                <span class="ses-ico ${statusClass(dataS)}" title="${escapeAttr(dataReason)}">${fileSvg}</span>
                ${dlBtn}
            </div>
            <div class="ses-ico-row">
                <span class="ses-ico ${statusClass(audioS)}" title="${escapeAttr(audioReason)}">${audioSvg}</span>
            </div>
            <div class="ses-ico-row">
                <span class="ses-ico ${statusClass(wxS)}" title="${escapeAttr(wxReason)}">${wxSvg}</span>
            </div>
        </div>
    `;
}

function sessionTypeFromName(name) {
    const n = (name || '').toLowerCase();
    if (n.includes('race') && !n.includes('sprint')) return 'race';
    if (n.includes('sprint qualifying') || n.includes('sprint shootout')) return 'qualifying';
    if (n.includes('sprint')) return 'race';
    if (n.includes('qualifying')) return 'qualifying';
    return 'practice';
}

function sessionUrlFor(year, ev, session, sessionType) {
    // Cache-key format used elsewhere: {year}_{round}_{Location}_{Session_Name}
    const id = `${year}_${ev.round}_${(ev.location||'').replace(/ /g, '_')}_${(session.name||'').replace(/ /g, '_')}`;
    return `/${sessionType}?session=${encodeURIComponent(id)}`;
}

// Open / close the session popover. Positioned just under the clicked
// event card so the rest of the grid stays put. A transparent backdrop
// catches outside-clicks for dismissal.
window.toggleEvent = function(evt, round) {
    const existing = document.querySelector('.session-popover');
    const existingBackdrop = document.querySelector('.session-popover-backdrop');
    const currentRound = existing ? parseInt(existing.dataset.round, 10) : null;
    if (existing) existing.remove();
    if (existingBackdrop) existingBackdrop.remove();
    if (currentRound === round) return;   // toggle off

    const ev = currentEvents.find(e => e.round === round);
    if (!ev) return;
    const card = (evt && evt.currentTarget) || document.querySelector(`.gp-card[data-round="${round}"]`);
    if (!card) return;

    const backdrop = document.createElement('div');
    backdrop.className = 'session-popover-backdrop';
    backdrop.onclick = closeSessionPopover;
    document.body.appendChild(backdrop);

    const pop = document.createElement('div');
    pop.className = 'session-popover';
    pop.dataset.round = String(round);
    pop.innerHTML = renderSessionPopoverHtml(currentYear, ev);
    // Anchor inside the schedule grid so absolute positioning is relative.
    const grid = document.getElementById('scheduleGrid');
    grid.appendChild(pop);

    // Position: just under the clicked card, left-aligned, but clamped
    // to stay inside the grid.
    const gridRect = grid.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    const top = (cardRect.bottom - gridRect.top) + 6;
    let left = cardRect.left - gridRect.left;
    const maxLeft = grid.clientWidth - pop.offsetWidth;
    if (left > maxLeft) left = Math.max(0, maxLeft);
    pop.style.top = `${top}px`;
    pop.style.left = `${left}px`;
};

function closeSessionPopover() {
    const p = document.querySelector('.session-popover');
    const b = document.querySelector('.session-popover-backdrop');
    if (p) p.remove();
    if (b) b.remove();
}
window.closeSessionPopover = closeSessionPopover;

window.downloadSession = async function(year, eventName, sessionName, btn) {
    btn.disabled = true;
    btn.textContent = 'Downloading…';
    try {
        const sessionType = sessionTypeFromName(sessionName);
        const resp = await fetch(`${API_BASE}/livetiming/fetch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year, meeting_name: eventName,
                session_type: sessionType,
            }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await loadCachedKeys();
        renderScheduleCards(currentYear, currentEvents);
        refreshCache();
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Retry';
        console.error(e);
    }
};

// Re-download a session's timing data (force=true), e.g. when the cached
// live.jsonl is incomplete or corrupted (card).
window.redownloadSession = async function(year, eventName, sessionName, btn) {
    btn.disabled = true;
    const prev = btn.innerHTML;
    btn.innerHTML = '…';
    try {
        const sessionType = sessionTypeFromName(sessionName);
        const resp = await fetch(`${API_BASE}/livetiming/fetch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year, meeting_name: eventName,
                session_type: sessionType, force: true,
            }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await loadCachedKeys();
        renderScheduleCards(currentYear, currentEvents);
        refreshCache();
        closeSessionPopover();
    } catch (e) {
        btn.disabled = false;
        btn.innerHTML = prev;
        console.error(e);
        alert(`Re-download failed: ${e.message}`);
    }
};

window.downloadAllSessions = async function(year, eventName, btn) {
    btn.disabled = true;
    btn.textContent = 'Downloading…';
    const ev = currentEvents.find(e => e.name === eventName);
    if (!ev) { btn.disabled = false; btn.textContent = 'Download all'; return; }
    for (const s of (ev.sessions || [])) {
        if (sessionIsCached(ev, s.name)) continue;
        try {
            const sessionType = sessionTypeFromName(s.name);
            await fetch(`${API_BASE}/livetiming/fetch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    year, meeting_name: eventName,
                    session_type: sessionType,
                }),
            });
        } catch (e) { console.error(e); }
    }
    await loadCachedKeys();
    renderScheduleCards(currentYear, currentEvents);
    refreshCache();
};

function escapeHtml(s) {
    return String(s || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escapeAttr(s) {
    return escapeHtml(s).replace(/'/g, '&#39;');
}

// =========================================================================
// Cache panel (sessions + total size)
// =========================================================================

window.refreshCache = async function() {
    const listEl = document.getElementById('cachedList');
    const countEl = document.getElementById('cachedCount');
    const sizeEl = document.getElementById('cachedSize');
    if (!listEl) return;
    try {
        const sessions = await fetchJSON(`${API_BASE}/livetiming/cached`);
        countEl.textContent = sessions.length;
        let totalBytes = 0;
        // Group by event (location) — most-recent event first, sessions
        // within an event also sorted most-recent first.
        const byEvent = {};
        for (const s of sessions) {
            // Server returns size as `size_mb` (megabytes).
            const mb = s.size_mb || 0;
            totalBytes += mb * 1024 * 1024;
            const loc = s.location || s.meeting || '?';
            if (!byEvent[loc]) byEvent[loc] = [];
            byEvent[loc].push(s);
        }
        sizeEl.textContent = formatBytes(totalBytes);
        // Event order: latest modified session in each event determines
        // the event's position.
        const eventsByRecency = Object.keys(byEvent).map(loc => {
            const ss = byEvent[loc];
            const latest = ss.map(s => s.modified || '').sort().slice(-1)[0] || '';
            return { loc, sessions: ss, latest };
        }).sort((a, b) => b.latest.localeCompare(a.latest));

        const html = eventsByRecency.map(g => {
            const sorted = g.sessions
                .slice()
                .sort((a, b) => (b.modified || '').localeCompare(a.modified || ''));
            const items = sorted.map(s => {
                const fullName = String(s.session || '').replace(/^\d+\s+/, '');
                const shortName = sessionShortName(fullName);
                const size = formatBytes((s.size_mb || 0) * 1024 * 1024);
                return `
                    <li title="${escapeHtml(fullName)}">
                        <span class="cache-li-name">${escapeHtml(shortName)}</span>
                        <span class="cache-li-size">${escapeHtml(size)}</span>
                        <button class="cache-li-del" title="Delete this session from disk"
                                onclick="deleteCachedSession('${escapeAttr(s.name)}', this)">×</button>
                    </li>
                `;
            }).join('');
            // Per-event delete-all: the session cache keys are passed so the
            // handler can loop the existing per-session DELETE endpoint.
            const keys = sorted.map(s => s.name);
            const keysAttr = escapeAttr(JSON.stringify(keys));
            return `
                <div class="cache-event">
                    <div class="cache-event-label">
                        <span>${escapeHtml(g.loc)}</span>
                        <button class="cache-event-del" title="Delete ALL sessions of this event"
                                onclick='deleteCachedEvent("${escapeAttr(g.loc)}", ${keysAttr}, this)'>×</button>
                    </div>
                    <ul>${items}</ul>
                </div>
            `;
        }).join('');
        listEl.innerHTML = html || '<div class="loading">No cached sessions.</div>';
    } catch (e) {
        listEl.innerHTML = '<div class="loading error">Failed to load cache info.</div>';
    }
};

function formatBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}

// Short session label for the compact cache list: FP1/FP2/FP3/SQ/Q/S/R.
function sessionShortName(name) {
    const n = (name || '').toLowerCase();
    if (n.includes('practice 1')) return 'FP1';
    if (n.includes('practice 2')) return 'FP2';
    if (n.includes('practice 3')) return 'FP3';
    if (n.includes('sprint qualifying') || n.includes('sprint shootout')) return 'SQ';
    if (n.includes('sprint')) return 'S';
    if (n.includes('qualifying')) return 'Q';
    if (n.includes('race')) return 'R';
    return name || '?';
}

window.deleteCachedSession = async function(sessionName, btn) {
    if (!confirm(`Delete ${sessionName} from disk?\n\n`
                 + `Audio and team radio cannot be re-downloaded later.`)) return;
    btn.disabled = true;
    try {
        const resp = await fetch(
            `${API_BASE}/livetiming/cached/${encodeURIComponent(sessionName)}`,
            { method: 'DELETE' }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await loadCachedKeys();
        renderScheduleCards(currentYear, currentEvents);
        await refreshCache();
    } catch (e) {
        btn.disabled = false;
        console.error(e);
        alert(`Delete failed: ${e.message}`);
    }
};

// Delete EVERY cached session of an event (loops the per-session DELETE).
window.deleteCachedEvent = async function(label, sessionKeys, btn) {
    if (!Array.isArray(sessionKeys) || !sessionKeys.length) return;
    if (!confirm(`Delete ALL ${sessionKeys.length} cached session(s) for ${label}?\n\n`
                 + `Audio and team radio cannot be re-downloaded later.`)) return;
    btn.disabled = true;
    try {
        for (const key of sessionKeys) {
            await fetch(`${API_BASE}/livetiming/cached/${encodeURIComponent(key)}`,
                        { method: 'DELETE' });
        }
        await loadCachedKeys();
        renderScheduleCards(currentYear, currentEvents);
        await refreshCache();
    } catch (e) {
        btn.disabled = false;
        console.error(e);
        alert(`Delete failed: ${e.message}`);
    }
};
