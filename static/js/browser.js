const API_BASE = '/api/v1';

// Format location name for cache key (just replace spaces with underscores)
// Note: Keep diacritics to match backend cache directory naming
function formatLocationKey(name) {
    return name.replace(/\s+/g, '_');
}

// Available years for F1 live timing data (fetched from API)
let AVAILABLE_YEARS = [];

// Cached sessions from server
let cachedSessions = [];

// Latest live session info (refreshed by refreshLiveBanner)
let liveSessionInfo = null;

// Preloaded meetings data per year (for accurate status calculation)
let meetingsCache = {};

// Current state
let state = {
    currentView: 'years',
    selectedYear: null,
    selectedMeeting: null,
    meetings: [],
};

// DOM Elements
const views = {
    years: document.getElementById('yearsView'),
    meetings: document.getElementById('meetingsView'),
    sessions: document.getElementById('sessionsView'),
};

const grids = {
    years: document.getElementById('yearsGrid'),
    meetings: document.getElementById('meetingsGrid'),
    sessions: document.getElementById('sessionsGrid'),
};

// Fetch available years from API
async function loadAvailableYears() {
    try {
        const response = await fetch(`${API_BASE}/years`);
        const data = await response.json();
        AVAILABLE_YEARS = data.years || [];
    } catch (error) {
        console.error('Failed to load years:', error);
        AVAILABLE_YEARS = [2026, 2025, 2024];  // Fallback
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    await loadAvailableYears();
    await loadCachedSessions();
    renderYears();
    // Preload meetings data for all years (for accurate status calculation)
    preloadAllMeetings();
    // Live session banner — refreshes every minute
    refreshLiveBanner();
    setInterval(refreshLiveBanner, 60000);
});

// =========================================================================
// Live Session Banner
// =========================================================================

async function refreshLiveBanner() {
    const banner = document.getElementById('liveBanner');
    if (!banner) return;
    try {
        const data = await fetchJSON(`${API_BASE}/schedule/live-session`);
        if (!data || !data.is_live) {
            liveSessionInfo = null;
            banner.classList.add('hidden');
            banner.innerHTML = '';
            // Re-render sessions in case the live session just ended.
            if (state.currentView === 'sessions') renderSessions();
            return;
        }
        liveSessionInfo = data;
        // Re-render sessions list so the live one swaps to Watch Live.
        if (state.currentView === 'sessions') renderSessions();

        // Build the same cache-key the timing client uses to identify the
        // session, so the live window opens against the right WS endpoint.
        const year = new Date().getFullYear();
        const meetingKey = data.meeting_key || 0;
        const location = (data.location || '').replace(/ /g, '_');
        const sessionType = (data.session_name || data.session_type || '').replace(/ /g, '_');
        const sessionKey = `${year}_${meetingKey}_${location}_${sessionType}`;
        const liveUrl = `/${data.page}?session=${encodeURIComponent(sessionKey)}&mode=live`;

        banner.classList.remove('hidden');
        banner.innerHTML = `
            <span class="live-badge">LIVE</span>
            <div class="live-info">
                <span class="live-title">${data.event_name || ''}</span>
                <span class="live-session">${data.session_name || data.session_type || ''}</span>
            </div>
            <button class="btn-watch-live" type="button">Watch Live</button>
        `;
        banner.querySelector('.btn-watch-live').onclick = () => {
            openTimingWindow(liveUrl, sessionKey);
        };
        // Whole banner clickable too
        banner.onclick = (e) => {
            if (e.target.closest('button')) return;
            openTimingWindow(liveUrl, sessionKey);
        };
    } catch (err) {
        // No live session endpoint or no session — hide silently.
        liveSessionInfo = null;
        banner.classList.add('hidden');
        banner.innerHTML = '';
    }
}

// True iff `session` (from the meetings list) is the currently-live session
// for `meeting`.
function isLiveSession(meeting, session) {
    if (!liveSessionInfo || !liveSessionInfo.is_live) return false;
    if (liveSessionInfo.meeting_key !== meeting.key) return false;
    // Compare by session_key when available, otherwise fall back to name.
    if (liveSessionInfo.session_key && session.key) {
        return liveSessionInfo.session_key === session.key;
    }
    const liveName = (liveSessionInfo.session_name || liveSessionInfo.session_type || '')
        .toLowerCase().trim();
    return liveName === (session.name || '').toLowerCase().trim();
}

function buildLiveUrls(meeting, session) {
    const route = liveSessionInfo.page
        ? `/${liveSessionInfo.page}`
        : (session.name.includes('Qualifying') || session.name.includes('Shootout')
            ? '/qualifying'
            : (session.name === 'Race' || session.name === 'Sprint' ? '/race' : '/practice'));
    const sessionType = (session.name || '').replace(/ /g, '_');
    const location = (meeting.location || '').replace(/ /g, '_');
    const sessionKey = `${state.selectedYear}_${meeting.key}_${location}_${sessionType}`;
    return {
        liveUrl: `${route}?session=${encodeURIComponent(sessionKey)}&mode=live`,
        startUrl: `${route}?session=${encodeURIComponent(sessionKey)}&mode=start`,
        sessionKey,
    };
}

// Same chrome-less popup behavior as startReplay() — keeps live & replay
// flows opening identically.
function openTimingWindow(url, sessionKey) {
    const features = [
    ].join(',');
    void sessionKey;
    // Open in the SAME tab — the timing page has a Close (X) button in
    // the header to go back to wherever we came from.
    window.location.href = url;
}

// Load cached sessions from server
async function loadCachedSessions() {
    try {
        const data = await fetchJSON(`${API_BASE}/livetiming/cached`);
        cachedSessions = data;
        renderCachedList();
        updateCacheStats();
    } catch (error) {
        console.error('Error loading cached sessions:', error);
    }
}

// Check if a session is cached.
// Primary match: by session key (from F1 API) if available.
// Fallback: by composite key (year_meetingKey_location_sessionName).
function isSessionCached(year, meetingKey, location, sessionName, sessionKey) {
    if (sessionKey) {
        const keyPrefix = `${sessionKey}_`;
        if (cachedSessions.some(s => s.session_key === String(sessionKey))) return true;
    }
    const normalizedLocation = formatLocationKey(location);
    const key = `${year}_${meetingKey}_${normalizedLocation}_${sessionName}`.replace(/ /g, '_');
    return cachedSessions.some(s => s.name === key);
}

// Get cached session info
function getCachedSession(year, meetingKey, location, sessionName, sessionKey) {
    if (sessionKey) {
        const found = cachedSessions.find(s => s.session_key === String(sessionKey));
        if (found) return found;
    }
    const normalizedLocation = formatLocationKey(location);
    const key = `${year}_${meetingKey}_${normalizedLocation}_${sessionName}`.replace(/ /g, '_');
    return cachedSessions.find(s => s.name === key);
}

// Get all cached sessions for a meeting (uses meeting key prefix)
function getCachedSessionsForMeeting(year, meetingKey, location) {
    const normalizedLocation = formatLocationKey(location);
    const prefix = `${year}_${meetingKey}_${normalizedLocation}`;
    return cachedSessions.filter(s => s.name.startsWith(prefix));
}

// Get all cached sessions for a year
function getCachedSessionsForYear(year) {
    return cachedSessions.filter(s => s.name.startsWith(`${year}_`));
}

// Preload meetings for all years (runs in background for status calculation)
async function preloadAllMeetings() {
    let loaded = false;
    for (const year of AVAILABLE_YEARS) {
        if (meetingsCache[year]) continue;
        try {
            const data = await fetchJSON(`${API_BASE}/livetiming/meetings/${year}`);
            meetingsCache[year] = data;
            loaded = true;
            // Re-render years view to update status indicators
            if (state.currentView === 'years') {
                renderYears();
            }
        } catch (error) {
            console.warn(`Failed to preload meetings for ${year}:`, error);
        }
    }
    // Reload cached sessions after preload — loading meetings triggers
    // migration of old-format cache directories on the backend
    if (loaded) {
        await loadCachedSessions();
        if (state.currentView === 'years') renderYears();
    }
}

// Check download status for a year
function getYearDownloadStatus(year) {
    const cached = getCachedSessionsForYear(year);
    if (cached.length === 0) return 'none';

    // If we have preloaded meetings data, calculate accurate status
    const meetings = meetingsCache[year];
    if (meetings) {
        let totalSessionsWithData = 0;
        let cachedCount = 0;
        for (const meeting of meetings) {
            const sessionsWithData = meeting.sessions.filter(s => s.has_data);
            totalSessionsWithData += sessionsWithData.length;
            cachedCount += getCachedSessionsForMeeting(year, meeting.key, meeting.location).length;
        }
        if (cachedCount >= totalSessionsWithData) return 'complete';
    }

    return 'partial';
}

// Check download status for a meeting
function getMeetingDownloadStatus(year, meetingKey, location, totalSessions) {
    const cached = getCachedSessionsForMeeting(year, meetingKey, location);
    if (cached.length === 0) return 'none';
    if (cached.length >= totalSessions) return 'complete';
    return 'partial';
}

// Navigation
function showView(viewName) {
    Object.values(views).forEach(v => v.classList.remove('active'));
    views[viewName].classList.add('active');
    state.currentView = viewName;
    updateBreadcrumb();
}

function updateBreadcrumb() {
    const breadcrumb = document.getElementById('breadcrumb');
    let html = `<span class="crumb ${state.currentView === 'years' ? 'active' : ''}" onclick="navigateTo('years')">Years</span>`;

    if (state.selectedYear) {
        html += `<span class="crumb ${state.currentView === 'meetings' ? 'active' : ''}" onclick="navigateTo('meetings')">${state.selectedYear}</span>`;
    }
    if (state.selectedMeeting) {
        html += `<span class="crumb active">${state.selectedMeeting.location}</span>`;
    }

    breadcrumb.innerHTML = html;
}

function navigateTo(view) {
    if (view === 'years') {
        state.selectedYear = null;
        state.selectedMeeting = null;
        renderYears();
    } else if (view === 'meetings') {
        state.selectedMeeting = null;
        renderMeetings();
    }
    showView(view);
}

// Render Functions
function renderYears() {
    const headerHtml = `
        <div class="view-header">
            <h2>Select Year</h2>
            <div class="view-actions">
                <button class="btn-download-all" onclick="downloadAllYears()" title="Download all available data">
                    <span class="icon">&#8595;</span> Download All
                </button>
            </div>
        </div>
    `;

    const yearsHtml = AVAILABLE_YEARS.map(year => {
        const status = getYearDownloadStatus(year);
        const cachedCount = getCachedSessionsForYear(year).length;

        let statusIcon = '';
        let statusClass = '';
        if (status === 'complete') {
            statusIcon = '<span class="status-icon complete">&#10003;</span>';
            statusClass = 'complete';
        } else if (status === 'partial') {
            statusIcon = '<span class="status-icon partial">&#9679;</span>';
            statusClass = 'partial';
        }

        return `
            <div class="card ${statusClass}" onclick="selectYear(${year})">
                ${statusIcon}
                <div class="card-title">${year}</div>
                <div class="card-subtitle">Season</div>
                ${cachedCount > 0 ? `<div class="card-meta">${cachedCount} sessions cached</div>` : ''}
            </div>
        `;
    }).join('');

    document.querySelector('#yearsView .view-header')?.remove();
    grids.years.innerHTML = yearsHtml;
    grids.years.insertAdjacentHTML('beforebegin', headerHtml);
}

async function selectYear(year) {
    state.selectedYear = year;
    document.getElementById('selectedYear').textContent = year;
    showView('meetings');

    // Use cached meetings if available
    if (meetingsCache[year]) {
        state.meetings = meetingsCache[year];
        renderMeetings();
        return;
    }

    grids.meetings.innerHTML = '<div class="loading-spinner"></div> Loading meetings...';

    try {
        const data = await fetchJSON(`${API_BASE}/livetiming/meetings/${year}`);
        state.meetings = data;
        meetingsCache[year] = data; // Cache for future use
        if (data.length === 0) {
            grids.meetings.innerHTML = `<div class="error">No meetings found for ${year}. The schedule may not be available yet.</div>`;
            return;
        }
        renderMeetings();
    } catch (error) {
        const errorMsg = error.message || 'Unknown error';
        if (errorMsg.includes('404') || errorMsg.includes('Not Found')) {
            grids.meetings.innerHTML = `<div class="error">No data available for ${year}. F1 Live Timing data may not exist for this year.</div>`;
        } else if (errorMsg.includes('500')) {
            grids.meetings.innerHTML = `<div class="error">Error loading ${year} data from F1 servers. The data may be temporarily unavailable or not yet published.</div>`;
        } else {
            grids.meetings.innerHTML = `<div class="error">Error loading meetings: ${errorMsg}</div>`;
        }
    }
}

function renderMeetings() {
    if (state.meetings.length === 0) {
        grids.meetings.innerHTML = '<div class="error">No meetings found for this year</div>';
        return;
    }

    const headerHtml = `
        <div class="view-header">
            <h2>Meetings in ${state.selectedYear}</h2>
            <div class="view-actions">
                <button class="btn-download-all" onclick="downloadAllMeetings()" title="Download all sessions for ${state.selectedYear}">
                    <span class="icon">&#8595;</span> Download All
                </button>
            </div>
        </div>
    `;

    const meetingsHtml = state.meetings.map((meeting, index) => {
        const sessionsWithData = meeting.sessions.filter(s => s.has_data).length;
        const status = getMeetingDownloadStatus(state.selectedYear, meeting.key, meeting.location, sessionsWithData);
        const cachedCount = getCachedSessionsForMeeting(state.selectedYear, meeting.key, meeting.location).length;

        let statusIcon = '';
        let statusClass = '';
        if (status === 'complete') {
            statusIcon = '<span class="status-icon complete">&#10003;</span>';
            statusClass = 'complete';
        } else if (status === 'partial') {
            statusIcon = '<span class="status-icon partial">&#9679;</span>';
            statusClass = 'partial';
        }

        const cachedText = cachedCount > 0 ? ` · ${cachedCount}/${sessionsWithData} cached` : '';

        return `
            <div class="card ${statusClass}" onclick="selectMeeting(${index})">
                ${statusIcon}
                <div class="card-title">${meeting.name}</div>
                <div class="card-subtitle">${meeting.location}, ${meeting.country}</div>
                <div class="card-meta">${meeting.circuit}${cachedText}</div>
            </div>
        `;
    }).join('');

    // Clear and re-render
    const existingHeader = document.querySelector('#meetingsView > .view-header');
    if (existingHeader) existingHeader.remove();

    grids.meetings.innerHTML = meetingsHtml;
    grids.meetings.insertAdjacentHTML('beforebegin', headerHtml);
}

function selectMeeting(index) {
    state.selectedMeeting = state.meetings[index];
    document.getElementById('selectedMeeting').textContent = `${state.selectedMeeting.name} ${state.selectedYear}`;
    showView('sessions');
    renderSessions();
}

function renderSessions() {
    const sessions = state.selectedMeeting.sessions;
    const sessionsWithData = sessions.filter(s => s.has_data);
    const meetingKey = state.selectedMeeting.key;
    const cachedCount = getCachedSessionsForMeeting(state.selectedYear, meetingKey, state.selectedMeeting.location).length;

    // Calculate remaining - only count sessions that have data AND aren't cached
    const remaining = sessionsWithData.filter(s =>
        !isSessionCached(state.selectedYear, meetingKey, state.selectedMeeting.location, s.name, s.key)
    ).length;

    const headerHtml = `
        <div class="view-header">
            <h2>${state.selectedMeeting.name} ${state.selectedYear}</h2>
            <div class="view-actions">
                <button class="btn-download-all" onclick="downloadAllSessions()" title="Download all sessions for this event" ${remaining === 0 ? 'disabled' : ''}>
                    <span class="icon">&#8595;</span> Download All (${remaining} remaining)
                </button>
            </div>
        </div>
    `;

    if (sessions.length === 0) {
        grids.sessions.innerHTML = '<div class="error">No sessions available for this meeting</div>';
        return;
    }

    const sessionsHtml = sessions.map(session => {
        const isCached = isSessionCached(state.selectedYear, meetingKey, state.selectedMeeting.location, session.name, session.key);
        const hasData = session.has_data;
        const cachedInfo = getCachedSession(state.selectedYear, meetingKey, state.selectedMeeting.location, session.name, session.key);
        const isLive = isLiveSession(state.selectedMeeting, session);

        let statusBadge = '';
        let actionButtons = '';
        let cardClass = 'session-card';

        if (isLive) {
            // Currently-live session: Watch Live + Watch from Start, no delete.
            const { liveUrl, startUrl, sessionKey } = buildLiveUrls(state.selectedMeeting, session);
            statusBadge = '<span class="card-status live"><span class="live-dot"></span> LIVE</span>';
            actionButtons = `
                <button class="btn-watch-live" onclick="event.stopPropagation(); openTimingWindow('${liveUrl}', '${sessionKey}')">
                    <span class="icon">&#9679;</span> Watch Live
                </button>
                <button class="btn-from-start" onclick="event.stopPropagation(); openTimingWindow('${startUrl}', '${sessionKey}')">
                    <span class="icon">&#9658;</span> From Start
                </button>
            `;
            cardClass += ' live';
        } else if (isCached) {
            statusBadge = '<span class="card-status downloaded"><span class="icon">&#10003;</span> Cached</span>';
            actionButtons = `
                <button class="btn-replay" onclick="event.stopPropagation(); startReplay('${escapeHtml(session.name)}')">
                    <span class="icon">&#9658;</span> Replay
                </button>
                <button class="btn-delete" onclick="event.stopPropagation(); deleteSession('${escapeHtml(session.name)}')">
                    <span class="icon">&#10005;</span>
                </button>
            `;
            cardClass += ' downloaded';
        } else if (hasData) {
            actionButtons = `
                <button class="btn-download" onclick="event.stopPropagation(); downloadSession('${escapeHtml(session.name)}')">
                    <span class="icon">&#8595;</span> Download
                </button>
            `;
        } else {
            statusBadge = '<span class="card-status unavailable">No data available</span>';
            cardClass += ' disabled';
        }

        const dateStr = session.start_date ? new Date(session.start_date).toLocaleDateString() : '';
        const sizeStr = cachedInfo ? `${cachedInfo.size_mb} MB` : '';

        return `
            <div class="card ${cardClass}">
                <div class="card-title">${session.name}</div>
                <div class="card-subtitle">${dateStr}</div>
                ${sizeStr ? `<div class="card-meta">${sizeStr}</div>` : ''}
                ${statusBadge}
                <div class="card-actions">
                    ${actionButtons}
                </div>
            </div>
        `;
    }).join('');

    // Clear and re-render
    const existingHeader = document.querySelector('#sessionsView > .view-header');
    if (existingHeader) existingHeader.remove();

    grids.sessions.innerHTML = sessionsHtml;
    grids.sessions.insertAdjacentHTML('beforebegin', headerHtml);
}

// Download functions
async function downloadSession(sessionName) {
    // Find the session and validate it has data
    const session = state.selectedMeeting.sessions.find(s => s.name === sessionName);
    if (!session) {
        showStatus(`Session not found: ${sessionName}`, 'error');
        return;
    }
    if (!session.has_data) {
        showStatus(`Session "${sessionName}" has no data available yet (session may not have started)`, 'warning');
        return;
    }

    const modal = document.getElementById('downloadModal');
    const progressText = document.getElementById('progressText');
    const progressFill = document.getElementById('progressFill');
    const progressTopics = document.getElementById('progressTopics');

    modal.classList.add('active');
    progressText.textContent = 'Connecting to F1 Live Timing...';
    progressFill.style.width = '0%';
    progressTopics.innerHTML = '';

    const year = state.selectedYear;
    const meetingName = state.selectedMeeting.location;
    const meetingKeyParam = state.selectedMeeting.key;

    try {
        // Pass session name as session_type - backend maps names like "Practice 1" correctly
        const params = new URLSearchParams({
            year: year,
            meeting_name: meetingName,
            session_type: sessionName,
            meeting_key: meetingKeyParam,
        });

        const eventSource = new EventSource(`${API_BASE}/livetiming/fetch/stream?${params}`);
        const topics = {};

        eventSource.addEventListener('start', (e) => {
            const data = JSON.parse(e.data);
            progressText.textContent = `Downloading ${data.location} ${data.session}...`;
        });

        eventSource.addEventListener('progress', (e) => {
            const data = JSON.parse(e.data);
            topics[data.topic] = data.status;

            const topicEntries = Object.entries(topics);
            const completed = topicEntries.filter(([_, s]) => s.startsWith('done') || s.startsWith('failed')).length;
            const total = topicEntries.length;
            const percent = total > 0 ? Math.round((completed / total) * 100) : 0;

            progressFill.style.width = `${percent}%`;
            progressText.textContent = `Downloading... ${completed}/${total} topics`;

            progressTopics.innerHTML = topicEntries.slice(-8).map(([topic, status]) => {
                const icon = status.startsWith('done') ? '&#10003;' :
                            status.startsWith('failed') ? '&#10007;' : '&#8987;';
                const cls = status.startsWith('done') ? 'done' :
                           status.startsWith('failed') ? 'failed' : 'pending';
                return `<div class="topic-status ${cls}"><span>${icon}</span> ${topic}</div>`;
            }).join('');
        });

        eventSource.addEventListener('complete', (e) => {
            eventSource.close();
            progressFill.style.width = '100%';
            progressText.textContent = 'Download complete!';

            setTimeout(() => {
                modal.classList.remove('active');
                loadCachedSessions().then(() => renderSessions());
            }, 1000);
        });

        eventSource.addEventListener('error', (e) => {
            eventSource.close();
            if (e.data) {
                progressText.textContent = `Error: ${e.data}`;
            } else {
                progressText.textContent = 'Connection error';
            }
            // Close modal after showing error briefly
            setTimeout(() => {
                modal.classList.remove('active');
            }, 3000);
        });

        eventSource.onerror = () => {
            eventSource.close();
            progressText.textContent = 'Connection lost';
            setTimeout(() => {
                modal.classList.remove('active');
            }, 2000);
        };

    } catch (error) {
        progressText.textContent = `Error: ${error.message}`;
        setTimeout(() => {
            modal.classList.remove('active');
        }, 3000);
    }
}

async function downloadAllSessions() {
    const mk = state.selectedMeeting.key;
    const sessions = state.selectedMeeting.sessions.filter(s => {
        const isCached = isSessionCached(state.selectedYear, mk, state.selectedMeeting.location, s.name, s.key);
        return s.has_data && !isCached;
    });

    if (sessions.length === 0) {
        showStatus('All sessions already downloaded', 'success');
        return;
    }

    const total = sessions.length;
    let completed = 0;
    let failed = 0;

    for (const session of sessions) {
        showStatus(`Downloading ${session.name} (${completed + 1}/${total})...`, 'info');
        const success = await downloadSessionSync(session.name);
        if (success) {
            completed++;
        } else {
            failed++;
        }
        // Update cache after each download so UI reflects progress
        await loadCachedSessions();
        renderSessions();
    }

    if (failed > 0) {
        showStatus(`Downloaded ${completed} sessions, ${failed} failed`, 'warning');
    } else {
        showStatus(`Downloaded ${completed} sessions`, 'success');
    }
}

async function downloadAllMeetings() {
    const uncachedMeetings = state.meetings.filter(meeting => {
        const sessionsWithData = meeting.sessions.filter(s => s.has_data);
        const status = getMeetingDownloadStatus(state.selectedYear, meeting.key, meeting.location, sessionsWithData.length);
        return status !== 'complete';
    });

    if (uncachedMeetings.length === 0) {
        showStatus('All meetings already downloaded', 'success');
        return;
    }

    let totalSessions = 0;
    for (const meeting of uncachedMeetings) {
        const uncached = meeting.sessions.filter(s => {
            return s.has_data && !isSessionCached(state.selectedYear, meeting.key, meeting.location, s.name);
        });
        totalSessions += uncached.length;
    }

    let completed = 0;
    let failed = 0;

    for (const meeting of uncachedMeetings) {
        const sessions = meeting.sessions.filter(s => {
            return s.has_data && !isSessionCached(state.selectedYear, meeting.key, meeting.location, s.name, s.key);
        });

        for (const session of sessions) {
            showStatus(`Downloading ${meeting.location} - ${session.name} (${completed + 1}/${totalSessions})...`, 'info');
            const success = await downloadSessionSyncForMeeting(state.selectedYear, meeting.location, session.name, meeting.key);
            if (success) {
                completed++;
            } else {
                failed++;
            }
        }
        // Update UI after each meeting
        await loadCachedSessions();
        renderMeetings();
    }

    if (failed > 0) {
        showStatus(`Downloaded ${completed} sessions, ${failed} failed`, 'warning');
    } else {
        showStatus(`Downloaded ${completed} sessions`, 'success');
    }
}

async function downloadAllYears() {
    showStatus('This will download ALL available F1 data. This may take a very long time.', 'warning');
    // For safety, don't auto-start this
}

async function downloadSessionSync(sessionName) {
    const year = state.selectedYear;
    const meetingName = state.selectedMeeting.location;
    return downloadSessionSyncForMeeting(year, meetingName, sessionName, state.selectedMeeting.key);
}

async function downloadSessionSyncForMeeting(year, meetingName, sessionName, meetingKey) {
    try {
        // Pass session name as session_type - backend maps names like "Practice 1" correctly
        const body = {
            year: year,
            meeting_name: meetingName,
            session_type: sessionName,
        };
        if (meetingKey != null) {
            body.meeting_key = meetingKey;
        }
        const response = await fetch(`${API_BASE}/livetiming/fetch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        return response.ok;
    } catch (error) {
        console.error(`Failed to download ${sessionName}:`, error);
        return false;
    }
}

// Delete session
async function deleteSession(sessionName) {
    const year = state.selectedYear;
    const meetingKey = state.selectedMeeting.key;
    const location = state.selectedMeeting.location;
    const normalizedLocation = formatLocationKey(location);

    const session = state.selectedMeeting.sessions.find(s => s.name === sessionName);
    const cachedInfo = getCachedSession(year, meetingKey, location, sessionName, session?.key);
    const sessionKey = cachedInfo ? cachedInfo.name : `${year}_${meetingKey}_${normalizedLocation}_${sessionName}`.replace(/ /g, '_');

    if (!confirm(`Delete cached data for ${sessionName}?`)) {
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/livetiming/cached/${encodeURIComponent(sessionKey)}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete');
        }

        showStatus(`Deleted ${sessionName}`, 'success');
        await loadCachedSessions();
        renderSessions();

    } catch (error) {
        showStatus(`Error: ${error.message}`, 'error');
    }
}

// Start replay
function startReplay(sessionName) {
    const year = state.selectedYear;
    const meetingKey = state.selectedMeeting.key;
    const location = state.selectedMeeting.location;
    const normalizedLocation = formatLocationKey(location);

    // Find the session object to get its key
    const session = state.selectedMeeting.sessions.find(s => s.name === sessionName);
    const cachedInfo = getCachedSession(year, meetingKey, location, sessionName, session?.key);
    // Use the actual cache key from the server (handles session key prefix correctly)
    const sessionKey = cachedInfo ? cachedInfo.name : `${year}_${meetingKey}_${normalizedLocation}_${sessionName}`.replace(/ /g, '_');

    // Determine which route to use based on session type
    // Default to practice (most common), then check for specific types
    let route = '/practice';
    if (sessionName.includes('Qualifying') || sessionName.includes('Shootout')) {
        route = '/qualifying';
    } else if (sessionName === 'Race' || sessionName === 'Sprint') {
        route = '/race';
    }

    const url = `${route}?session=${encodeURIComponent(sessionKey)}`;
    openTimingWindow(url, sessionKey);
}

// Cached list rendering
function renderCachedList() {
    const list = document.getElementById('cachedList');

    if (cachedSessions.length === 0) {
        list.innerHTML = '<div class="empty-cache">No sessions cached</div>';
        return;
    }

    const recent = cachedSessions.slice(0, 5);
    list.innerHTML = recent.map(session => `
        <div class="cached-item" onclick="navigateToCached('${session.name}')">
            <div class="cached-name">${session.name.replace(/_/g, ' ')}</div>
            <div class="cached-size">${session.size_mb} MB</div>
        </div>
    `).join('');
}

function navigateToCached(sessionName) {
    // Format: year_meetingKey_location_session (e.g. 2025_1229_Melbourne_Practice_1)
    const parts = sessionName.split('_');
    if (parts.length >= 4) {
        const year = parseInt(parts[0]);
        const meetingKey = parseInt(parts[1]);
        const location = parts[2];

        selectYear(year).then(() => {
            // Find meeting by unique API key (most reliable) or by location
            const meeting = state.meetings.find(m => m.key === meetingKey) ||
                           state.meetings.find(m => m.location.replace(/ /g, '_') === location);
            if (meeting) {
                const index = state.meetings.indexOf(meeting);
                selectMeeting(index);
            }
        });
    }
}

function updateCacheStats() {
    const count = cachedSessions.length;
    const totalSize = cachedSessions.reduce((sum, s) => sum + (s.size_mb || 0), 0);

    document.getElementById('cachedCount').textContent = count;
    document.getElementById('cachedSize').textContent =
        totalSize >= 1024
            ? `${(totalSize / 1024).toFixed(1)} GB`
            : `${totalSize.toFixed(1)} MB`;
}

async function refreshCache() {
    await loadCachedSessions();

    // Re-render current view
    if (state.currentView === 'years') {
        renderYears();
    } else if (state.currentView === 'meetings') {
        renderMeetings();
    } else if (state.currentView === 'sessions') {
        renderSessions();
    }

    showStatus('Cache refreshed', 'success');
}

// Utility functions
async function fetchJSON(url) {
    const response = await fetch(url);
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML.replace(/'/g, "\\'");
}

let statusTimeout = null;

function showStatus(message, type = 'info') {
    const status = document.getElementById('downloadStatus');
    status.textContent = message;
    status.className = `download-status active ${type}`;

    if (statusTimeout) {
        clearTimeout(statusTimeout);
    }

    statusTimeout = setTimeout(() => {
        status.classList.remove('active');
    }, 4000);
}

function closeModal() {
    document.getElementById('downloadModal').classList.remove('active');
}

document.getElementById('downloadModal').addEventListener('click', (e) => {
    if (e.target.id === 'downloadModal') closeModal();
});
