const API_BASE = '/api/v1';

let replayState = {
    replayId: null,
    status: 'loading',
    speed: 10,
    isPlaying: false,
    currentFrame: 0,
    totalFrames: 0,
    framesLoaded: 0,
    estimatedTotalLaps: null,
    drivers: {},
    tyreHistory: {},  // Track tyre stints per driver
};

let eventSource = null;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    // Check URL params for session info
    const params = new URLSearchParams(window.location.search);
    const year = params.get('year');
    const race = params.get('race');
    const session = params.get('session');

    if (year && race && session) {
        startReplay(parseInt(year), race, session);
    } else {
        showSessionSelector();
    }

    // Add click listener for progress bar seeking
    const progressBar = document.querySelector('.progress-bar');
    if (progressBar) {
        progressBar.addEventListener('click', handleProgressBarClick);
        progressBar.style.cursor = 'pointer';
    }

    // Set initial speed button state
    document.querySelectorAll('.btn-speed').forEach(btn => {
        btn.classList.toggle('active', btn.textContent === `${replayState.speed}x`);
    });
});

async function startReplay(year, race, sessionType) {
    try {
        document.getElementById('eventName').textContent = `${year} ${race}`;
        document.getElementById('sessionName').textContent = sessionType;

        const response = await fetch(`${API_BASE}/replay/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year: year,
                race: race,
                session_type: sessionType,
                speed: replayState.speed,
            }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to start replay');
        }

        const data = await response.json();
        replayState.replayId = data.replay_id;

        // Connect to SSE stream
        connectToStream(data.replay_id);

    } catch (error) {
        showError(`Error starting replay: ${error.message}`);
    }
}

let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;

function connectToStream(replayId) {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource(`${API_BASE}/replay/${replayId}/stream`);

    eventSource.addEventListener('frame', (e) => {
        reconnectAttempts = 0; // Reset on successful message
        const data = JSON.parse(e.data);
        handleFrameUpdate(data);
    });

    eventSource.addEventListener('done', (e) => {
        const data = JSON.parse(e.data);
        handleFrameUpdate(data);
        replayState.isPlaying = false;
        updatePlayPauseButton();
    });

    eventSource.onerror = async (error) => {
        console.error('SSE connection error:', error);
        eventSource.close();

        reconnectAttempts++;
        if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
            console.error('Max reconnect attempts reached, checking if replay exists...');
            await checkAndRecoverReplay();
            return;
        }

        // Exponential backoff: 1s, 2s, 4s, 8s, 16s
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 16000);
        console.log(`Reconnecting in ${delay}ms (attempt ${reconnectAttempts})...`);

        setTimeout(() => {
            if (replayState.replayId) {
                connectToStream(replayState.replayId);
            }
        }, delay);
    };
}

async function checkAndRecoverReplay() {
    if (!replayState.replayId) return;

    try {
        const response = await fetch(`${API_BASE}/replay/${replayState.replayId}`);
        if (response.ok) {
            // Replay exists, try reconnecting
            reconnectAttempts = 0;
            connectToStream(replayState.replayId);
        } else if (response.status === 404) {
            // Replay was cleaned up, offer to restart
            console.log('Replay no longer exists, restarting...');
            showReplayLostMessage();
        }
    } catch (error) {
        console.error('Failed to check replay status:', error);
        showError('Connection lost. Please refresh the page.');
    }
}

function showReplayLostMessage() {
    const tower = document.getElementById('towerBody');
    tower.innerHTML = `
        <div class="error-message">
            <p>Session expired. Would you like to restart?</p>
            <button class="btn-primary" onclick="restartReplay()">Restart Replay</button>
            <a href="/browser" class="btn-back">Back to Browser</a>
        </div>
    `;
}

async function restartReplay() {
    const params = new URLSearchParams(window.location.search);
    const year = params.get('year');
    const race = params.get('race');
    const session = params.get('session');

    if (year && race && session) {
        reconnectAttempts = 0;
        await startReplay(parseInt(year), race, session);
    } else {
        window.location.href = '/browser';
    }
}

function handleFrameUpdate(data) {
    replayState.status = data.status;
    replayState.currentFrame = data.current_frame;
    replayState.totalFrames = data.total_frames;
    replayState.framesLoaded = data.frames_loaded || data.total_frames;
    replayState.estimatedTotalLaps = data.estimated_total_laps;

    // Update UI
    document.getElementById('eventName').textContent = data.event_name || replayState.eventName;
    document.getElementById('sessionName').textContent = data.session_name || '';

    // Update frame counter
    const frameCounter = document.getElementById('frameCounter');
    if (data.status === 'streaming') {
        frameCounter.textContent = `${data.current_frame}/${data.frames_loaded}`;
    } else {
        frameCounter.textContent = `${data.current_frame}/${data.total_frames}`;
    }

    // Update lap counter
    const lapCounter = document.getElementById('lapCounter');
    if (data.total_laps) {
        lapCounter.textContent = `${data.current_frame}/${data.total_laps}`;
    } else {
        lapCounter.textContent = `${data.current_frame}/-`;
    }

    // Update progress bar with dual indicators
    updateProgressBar(data);

    // Render during streaming, ready, playing, or paused
    if (data.status === 'streaming' || data.status === 'ready' ||
        data.status === 'playing' || data.status === 'paused') {
        if (data.frame) {
            // Update session time
            document.getElementById('sessionTime').textContent = data.frame.session_time || '00:00:00';

            // Update flag status
            updateFlagStatus(data.frame.flag_status);

            // Update timing tower
            renderTimingTower(data.frame.positions);

            // Update race control messages
            renderRaceControlMessages(data.frame.race_control_messages);

            // Update tyre strategy
            updateTyreHistory(data.frame.positions);
            renderTyreStrategy();

            // Update weather
            renderWeather(data.frame.weather);
        }
    }

    // Handle status changes
    if (data.status === 'streaming') {
        // Still loading, show streaming indicator
        updateLoadingIndicator(data.frames_loaded, data.estimated_total_laps);
    } else if (data.status === 'ready' && !replayState.isPlaying) {
        // All frames loaded
        hideLoadingIndicator();
    } else if (data.status === 'error') {
        showError(data.error || 'Replay error');
    }
}

function updateProgressBar(data) {
    const progressFill = document.getElementById('progressFill');
    const progressLoaded = document.getElementById('progressLoaded');

    // Calculate current position progress
    const currentProgress = data.frames_loaded > 0
        ? (data.current_frame / data.frames_loaded) * 100
        : 0;
    progressFill.style.width = `${currentProgress}%`;

    // Show loaded extent during streaming
    if (progressLoaded) {
        if (data.status === 'streaming' && data.estimated_total_laps) {
            const loadedProgress = (data.frames_loaded / data.estimated_total_laps) * 100;
            progressLoaded.style.width = `${Math.min(loadedProgress, 100)}%`;
            progressLoaded.style.display = 'block';
        } else {
            progressLoaded.style.display = 'none';
        }
    }
}

function updateLoadingIndicator(framesLoaded, estimatedTotal) {
    let indicator = document.getElementById('streamingIndicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'streamingIndicator';
        indicator.className = 'streaming-indicator';
        document.querySelector('.top-bar-center')?.appendChild(indicator);
    }

    let text = `Loading: ${framesLoaded}`;
    if (estimatedTotal) {
        text += `/${estimatedTotal}`;
    }
    indicator.textContent = text;
    indicator.style.display = 'block';
}

function hideLoadingIndicator() {
    const indicator = document.getElementById('streamingIndicator');
    if (indicator) {
        indicator.style.display = 'none';
    }
    document.querySelector('.loading-message')?.remove();
}

function renderTimingTower(positions) {
    if (!positions || positions.length === 0) return;

    const tower = document.getElementById('towerBody');
    const sessionName = document.getElementById('sessionName')?.textContent || '';
    const isRace = sessionName.toLowerCase().includes('race') || sessionName.toLowerCase().includes('sprint');

    // Update column headers based on session type
    const gapHeader = document.getElementById('colGapHeader');
    const intHeader = document.getElementById('colIntHeader');
    if (gapHeader && intHeader) {
        if (isRace) {
            gapHeader.textContent = 'GAP';
            intHeader.textContent = 'INT';
        } else {
            gapHeader.textContent = 'TIME';
            intHeader.textContent = 'GAP';
        }
    }

    tower.innerHTML = positions.map(entry => {
        const pitClass = entry.pit ? 'pit' : '';
        const outClass = entry.out ? 'out' : '';

        // For quali/practice: show lap time in col1, gap in col2
        // For race: show gap in col1, interval in col2
        const col1 = isRace ? entry.gap : (entry.last_lap || '-');
        const col2 = isRace ? entry.interval : entry.gap;

        return `
            <div class="tower-row ${pitClass} ${outClass}">
                <span class="col-pos">${entry.pos}</span>
                <span class="col-driver">
                    <span class="driver-color" style="--team-color: ${entry.color}"></span>
                    <span class="driver-abbr">${entry.driver}</span>
                </span>
                <span class="col-gap">${col1}</span>
                <span class="col-int">${col2}</span>
            </div>
        `;
    }).join('');
}

function renderRaceControlMessages(messages) {
    const container = document.getElementById('raceControlBody');
    if (!container) return;

    if (!messages || messages.length === 0) {
        container.innerHTML = '<div class="rc-message-placeholder">No messages</div>';
        return;
    }

    // Show messages in reverse order (newest first)
    const reversedMessages = [...messages].reverse();

    container.innerHTML = reversedMessages.map(msg => {
        let flagClass = '';
        if (msg.flag) {
            if (msg.flag.toUpperCase().includes('YELLOW')) flagClass = 'flag-yellow';
            else if (msg.flag.toUpperCase().includes('RED')) flagClass = 'flag-red';
        }

        return `
            <div class="rc-message ${flagClass}">
                <div class="rc-message-time">${msg.time}</div>
                <div class="rc-message-text">${msg.message}</div>
            </div>
        `;
    }).join('');
}

function updateTyreHistory(positions) {
    if (!positions) return;

    const currentLap = positions[0]?.lap || 0;

    positions.forEach(entry => {
        if (!replayState.tyreHistory[entry.driver]) {
            replayState.tyreHistory[entry.driver] = [];
        }

        const history = replayState.tyreHistory[entry.driver];
        const lastStint = history[history.length - 1];

        // If no history or tyre changed, add new stint
        if (!lastStint || lastStint.compound !== entry.tyre) {
            if (lastStint) {
                lastStint.endLap = currentLap - 1;
            }
            if (entry.tyre) {
                history.push({
                    compound: entry.tyre,
                    startLap: currentLap - entry.tyre_age + 1,
                    endLap: null,  // Still on this tyre
                });
            }
        }
    });
}

function renderTyreStrategy() {
    const container = document.getElementById('tyreStrategyBody');
    if (!container) return;

    const totalLaps = replayState.estimatedTotalLaps || replayState.totalFrames || 1;

    // Get drivers in current position order (from timing tower)
    const towerRows = document.querySelectorAll('.tower-row');
    const driverOrder = Array.from(towerRows).map(row => {
        const abbr = row.querySelector('.driver-abbr');
        return abbr ? abbr.textContent : null;
    }).filter(Boolean);

    let html = '';

    driverOrder.forEach(driver => {
        const stints = replayState.tyreHistory[driver];
        if (!stints || stints.length === 0) return;

        let stintsHtml = '';
        stints.forEach(stint => {
            const startLap = Math.max(1, stint.startLap);
            const endLap = stint.endLap || replayState.currentFrame;
            const widthPercent = ((endLap - startLap + 1) / totalLaps) * 100;
            const compound = stint.compound?.toLowerCase() || 'unknown';

            // Get tyre color
            let tyreColor = '#666';
            switch (compound) {
                case 'soft': tyreColor = 'var(--tyre-soft)'; break;
                case 'medium': tyreColor = 'var(--tyre-medium)'; break;
                case 'hard': tyreColor = 'var(--tyre-hard)'; break;
                case 'intermediate': tyreColor = 'var(--tyre-inter)'; break;
                case 'wet': tyreColor = 'var(--tyre-wet)'; break;
            }

            const lapsOnTyre = endLap - startLap + 1;
            stintsHtml += `
                <div class="tyre-stint" style="--stint-width: ${widthPercent}%; --stint-color: ${tyreColor}" title="${stint.compound}: Lap ${startLap}-${endLap}">
                    ${lapsOnTyre > 3 ? lapsOnTyre : ''}
                </div>
            `;
        });

        html += `
            <div class="tyre-row">
                <span class="tyre-driver">${driver}</span>
                <div class="tyre-bar">${stintsHtml}</div>
            </div>
        `;
    });

    container.innerHTML = html || '<div class="rc-message-placeholder">No tyre data</div>';
}

function renderWeather(weather) {
    if (!weather) return;

    const airTemp = document.getElementById('airTemp');
    const trackTemp = document.getElementById('trackTemp');
    const windSpeed = document.getElementById('windSpeed');
    const rainfall = document.getElementById('rainfall');

    if (airTemp && weather.air_temp !== null) {
        airTemp.textContent = `${weather.air_temp.toFixed(1)}°C`;
    }
    if (trackTemp && weather.track_temp !== null) {
        trackTemp.textContent = `${weather.track_temp.toFixed(1)}°C`;
    }
    if (windSpeed && weather.wind_speed !== null) {
        let windText = `${weather.wind_speed.toFixed(1)} km/h`;
        if (weather.wind_direction !== null) {
            windText += ` ${getWindDirection(weather.wind_direction)}`;
        }
        windSpeed.textContent = windText;
    }
    if (rainfall) {
        rainfall.textContent = weather.rainfall ? 'Yes' : 'No';
        rainfall.style.color = weather.rainfall ? 'var(--blue)' : 'var(--green)';
    }
}

function getWindDirection(degrees) {
    const directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
    const index = Math.round(degrees / 45) % 8;
    return directions[index];
}

function updateFlagStatus(status) {
    const indicator = document.getElementById('flagIndicator');
    const text = document.getElementById('flagText');

    if (!indicator || !text) return;

    indicator.className = `flag-indicator flag-${status.toLowerCase()}`;
    text.textContent = status;
}

async function togglePlayPause() {
    if (!replayState.replayId) return;

    const action = replayState.isPlaying ? 'pause' : 'play';

    try {
        const response = await fetch(`${API_BASE}/replay/${replayState.replayId}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
        });

        if (response.ok) {
            const data = await response.json();
            handleControlResponse(data);
            replayState.isPlaying = action === 'play';
            updatePlayPauseButton();
        } else if (response.status === 404) {
            showReplayLostMessage();
        } else {
            console.error('Control failed:', response.status);
        }
    } catch (error) {
        console.error('Control error:', error);
    }
}

function updatePlayPauseButton() {
    const icon = document.getElementById('playPauseIcon');
    icon.innerHTML = replayState.isPlaying ? '&#10074;&#10074;' : '&#9658;';
}

async function setSpeed(speed) {
    replayState.speed = speed;

    // Update UI
    document.querySelectorAll('.btn-speed').forEach(btn => {
        btn.classList.toggle('active', btn.textContent === `${speed}x`);
    });

    if (!replayState.replayId) return;

    try {
        const response = await fetch(`${API_BASE}/replay/${replayState.replayId}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'speed', value: speed }),
        });
        if (response.status === 404) {
            showReplayLostMessage();
        }
    } catch (error) {
        console.error('Speed control error:', error);
    }
}

async function seekByFrames(delta) {
    if (!replayState.replayId) return;

    const newFrame = Math.max(0, Math.min(
        replayState.currentFrame + delta,
        replayState.framesLoaded - 1
    ));

    await seekToFrame(newFrame);
}

async function seekToFrame(frameIndex) {
    if (!replayState.replayId) return;

    // Clamp to loaded frames
    const maxFrame = replayState.framesLoaded - 1;
    frameIndex = Math.max(0, Math.min(frameIndex, maxFrame));

    try {
        const response = await fetch(`${API_BASE}/replay/${replayState.replayId}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'seek', value: frameIndex }),
        });
        if (response.ok) {
            const data = await response.json();
            handleControlResponse(data);
        } else if (response.status === 404) {
            showReplayLostMessage();
        }
    } catch (error) {
        console.error('Seek error:', error);
    }
}

// Handle control action responses (same format as SSE frame events)
function handleControlResponse(data) {
    handleFrameUpdate(data);
}

function handleProgressBarClick(event) {
    const progressBar = event.currentTarget;
    const rect = progressBar.getBoundingClientRect();
    const clickX = event.clientX - rect.left;
    const percentage = clickX / rect.width;

    // Calculate frame index based on loaded frames
    const frameIndex = Math.floor(percentage * replayState.framesLoaded);
    seekToFrame(frameIndex);
}

function showError(message) {
    const tower = document.getElementById('towerBody');
    tower.innerHTML = `
        <div class="error-message">
            <p>${message}</p>
            <a href="/browser" class="btn-back">Back to Browser</a>
        </div>
    `;
}

function showSessionSelector() {
    // Show modal to select a session
    const modal = document.getElementById('sessionModal');
    const body = document.getElementById('sessionModalBody');

    body.innerHTML = `
        <p>No session specified. Please select a session from the <a href="/browser">browser</a>.</p>
        <div class="modal-actions">
            <a href="/browser" class="btn-primary">Go to Browser</a>
        </div>
    `;

    modal.classList.add('active');
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (eventSource) {
        eventSource.close();
    }
});
