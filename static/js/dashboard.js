const API_BASE = '/api/v1';

const elements = {
    year: document.getElementById('year'),
    race: document.getElementById('race'),
    session: document.getElementById('session'),
    driver: document.getElementById('driver'),
    loadBtn: document.getElementById('loadBtn'),
    loading: document.getElementById('loading'),
    lapTableBody: document.getElementById('lapTableBody'),
    fastestTableBody: document.getElementById('fastestTableBody'),
    fastestLap: document.getElementById('fastestLap'),
    fastestDriver: document.getElementById('fastestDriver'),
    totalLaps: document.getElementById('totalLaps'),
};

async function fetchJSON(url) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
    }
    return response.json();
}

async function loadSchedule(year) {
    try {
        const data = await fetchJSON(`${API_BASE}/schedule/${year}`);
        elements.race.innerHTML = '<option value="">Select a race...</option>';

        data.events.forEach(event => {
            const option = document.createElement('option');
            option.value = event.name;
            option.textContent = `${event.round}. ${event.name}`;
            elements.race.appendChild(option);
        });
    } catch (error) {
        console.error('Error loading schedule:', error);
    }
}

function getTyreClass(compound) {
    if (!compound) return '';
    const tyreMap = {
        'SOFT': 'tyre-soft',
        'MEDIUM': 'tyre-medium',
        'HARD': 'tyre-hard',
        'INTERMEDIATE': 'tyre-intermediate',
        'WET': 'tyre-wet',
    };
    return tyreMap[compound.toUpperCase()] || '';
}

function formatLapTime(timeStr) {
    if (!timeStr || timeStr === 'None' || timeStr === 'NaT') return '--';

    if (timeStr.includes('days')) {
        const match = timeStr.match(/(\d+):(\d+):(\d+\.?\d*)/);
        if (match) {
            const [, hours, mins, secs] = match;
            if (hours === '0' && mins === '0') {
                return parseFloat(secs).toFixed(3);
            }
            return `${mins}:${parseFloat(secs).toFixed(3).padStart(6, '0')}`;
        }
    }
    return timeStr;
}

async function loadLapData() {
    const year = elements.year.value;
    const race = elements.race.value;
    const session = elements.session.value;
    const driver = elements.driver.value;

    if (!race) {
        alert('Please select a race');
        return;
    }

    elements.loading.classList.add('active');
    elements.loadBtn.disabled = true;
    elements.lapTableBody.innerHTML = '';
    elements.fastestTableBody.innerHTML = '';

    try {
        const driverParam = driver ? `?driver=${driver}` : '';
        const [lapsData, fastestData] = await Promise.all([
            fetchJSON(`${API_BASE}/sessions/${year}/${race}/${session}/laps${driverParam}`),
            fetchJSON(`${API_BASE}/sessions/${year}/${race}/${session}/fastest-laps`),
        ]);

        renderLapTable(lapsData.laps);
        renderFastestTable(fastestData.fastest_laps);
        updateStats(lapsData.laps, fastestData.fastest_laps);
        updateDriverFilter(fastestData.fastest_laps);

    } catch (error) {
        console.error('Error loading data:', error);
        elements.lapTableBody.innerHTML = `
            <tr><td colspan="7" class="error">Error loading data: ${error.message}</td></tr>
        `;
    } finally {
        elements.loading.classList.remove('active');
        elements.loadBtn.disabled = false;
    }
}

function renderLapTable(laps) {
    if (!laps || laps.length === 0) {
        elements.lapTableBody.innerHTML = '<tr><td colspan="7">No lap data available</td></tr>';
        return;
    }

    const fastestTime = laps
        .filter(l => l.lap_time && l.lap_time !== 'NaT')
        .reduce((min, l) => (!min || l.lap_time < min) ? l.lap_time : min, null);

    elements.lapTableBody.innerHTML = laps.map(lap => {
        const isFastest = lap.lap_time === fastestTime;
        const tyreClass = getTyreClass(lap.compound);

        return `
            <tr>
                <td>${lap.lap_number}</td>
                <td>${lap.driver}</td>
                <td class="lap-time ${isFastest ? 'fastest' : ''}">${formatLapTime(lap.lap_time)}</td>
                <td class="lap-time">${formatLapTime(lap.sector1)}</td>
                <td class="lap-time">${formatLapTime(lap.sector2)}</td>
                <td class="lap-time">${formatLapTime(lap.sector3)}</td>
                <td class="${tyreClass}">${lap.compound || '--'}</td>
            </tr>
        `;
    }).join('');
}

function renderFastestTable(fastestLaps) {
    if (!fastestLaps || fastestLaps.length === 0) {
        elements.fastestTableBody.innerHTML = '<tr><td colspan="5">No data available</td></tr>';
        return;
    }

    elements.fastestTableBody.innerHTML = fastestLaps.map((lap, index) => {
        const posClass = index < 3 ? `position-${index + 1}` : '';
        const tyreClass = getTyreClass(lap.compound);

        return `
            <tr>
                <td class="position ${posClass}">${index + 1}</td>
                <td>${lap.driver}</td>
                <td class="lap-time">${formatLapTime(lap.lap_time)}</td>
                <td>${lap.lap_number}</td>
                <td class="${tyreClass}">${lap.compound || '--'}</td>
            </tr>
        `;
    }).join('');
}

function updateStats(laps, fastestLaps) {
    if (fastestLaps && fastestLaps.length > 0) {
        elements.fastestLap.textContent = formatLapTime(fastestLaps[0].lap_time);
        elements.fastestDriver.textContent = fastestLaps[0].driver;
    } else {
        elements.fastestLap.textContent = '--';
        elements.fastestDriver.textContent = '--';
    }

    elements.totalLaps.textContent = laps ? laps.length : 0;
}

function updateDriverFilter(fastestLaps) {
    const currentValue = elements.driver.value;
    elements.driver.innerHTML = '<option value="">All drivers</option>';

    if (fastestLaps) {
        fastestLaps.forEach(lap => {
            const option = document.createElement('option');
            option.value = lap.driver;
            option.textContent = lap.driver;
            elements.driver.appendChild(option);
        });
    }

    elements.driver.value = currentValue;
}

// Event listeners
elements.year.addEventListener('change', () => loadSchedule(elements.year.value));
elements.loadBtn.addEventListener('click', loadLapData);
elements.race.addEventListener('change', () => {
    elements.driver.innerHTML = '<option value="">All drivers</option>';
});

// Initialize
loadSchedule(elements.year.value);
