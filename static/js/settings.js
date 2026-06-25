/* Settings dialog (card 27).
 *
 * Page-agnostic: builds the modal on demand, loads/saves via /api/v1/settings,
 * and exposes window.F1_SETTINGS for other components (team-radio auto-play,
 * favourite drivers/teams). Any element with class `open-settings` opens it.
 */
(function () {
    const API = '/api/v1/settings';
    const TYPES = ['practice', 'qualifying', 'race'];
    let current = null;

    async function load() {
        try {
            current = await (await fetch(API)).json();
        } catch (e) {
            current = null;
        }
        window.F1_SETTINGS = current || {};
        return window.F1_SETTINGS;
    }

    const esc = (s) => String(s == null ? '' : s).replace(/"/g, '&quot;');
    const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
    const FOLDER_SVG = '<svg viewBox="0 0 24 24"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>';

    function perTypeRow(key, label, vals) {
        vals = vals || {};
        const boxes = TYPES.map((t) =>
            `<label class="set-pt"><input type="checkbox" data-key="${key}.${t}" ${vals[t] ? 'checked' : ''}><span>${cap(t)}</span></label>`
        ).join('');
        return `<div class="set-row set-pt-row"><span class="set-label">${label}</span><span class="set-pt-boxes">${boxes}</span></div>`;
    }
    const boolRow = (key, label, val) =>
        `<div class="set-row"><label class="set-check"><input type="checkbox" data-key="${key}" ${val ? 'checked' : ''}><span>${label}</span></label></div>`;
    const textRow = (key, label, val, ph) =>
        `<div class="set-row"><label class="set-label" for="set-${key}">${label}</label><input type="text" id="set-${key}" data-key="${key}" value="${esc(val)}" placeholder="${esc(ph || '')}"></div>`;
    const numRow = (key, label, val) =>
        `<div class="set-row"><label class="set-label" for="set-${key}">${label}</label><input type="number" id="set-${key}" data-key="${key}" value="${esc(val)}"></div>`;

    function buildForm(s) {
        const n = s.ntfy || {}, a = s.alerts || {}, au = s.auth || {};
        return `
        <div class="set-section"><h4>General</h4>
            ${boolRow('debug', 'Debug — keep transient/ephemeral files', s.debug)}
            <div class="set-row">
                <span class="set-label">Cache location</span>
                <span class="set-cachedir">
                    <input type="text" id="set-cacheDir-display" value="${esc(s._cacheDir || '')}" readonly>
                    <button class="set-folder-btn" id="pickCacheDir" title="Choose folder" aria-label="Choose folder">${FOLDER_SVG}</button>
                </span>
            </div>
            ${textRow('rainbowAiApiKey', 'Rainbow.ai API key (weather radar)', s.rainbowAiApiKey)}
        </div>
        <div class="set-section"><h4>Audio &amp; team radio</h4>
            ${perTypeRow('audio', 'Download &amp; play commentary', s.audio)}
            ${perTypeRow('teamRadio', 'Download team radio', s.teamRadio)}
            ${perTypeRow('keepFiles', 'Keep downloaded files', s.keepFiles)}
            ${boolRow('teamRadioAutoplay', 'Auto-play team radio (mutes commentary)', s.teamRadioAutoplay)}
        </div>
        <div class="set-section"><h4>Notifications</h4>
            ${textRow('ntfy.webhookUrl', 'Webhook URL (ntfy / Discord / Slack)', n.webhookUrl)}
            ${boolRow('ntfy.sessionLive', 'Notify when a session goes live', n.sessionLive)}
            ${boolRow('ntfy.preSession', 'Notify before a session', n.preSession)}
            ${numRow('ntfy.preSessionLeadMinutes', 'Minutes before session', n.preSessionLeadMinutes)}
            ${boolRow('ntfy.tokenExpiry', 'Notify on F1 token expiry', n.tokenExpiry)}
            ${boolRow('ntfy.repeat', 'Repeat notifications', n.repeat)}
        </div>
        <div class="set-section"><h4>Alerts</h4>
            ${textRow('alerts.favouriteDrivers', 'Favourite drivers (TLAs)', (a.favouriteDrivers || []).join(', '), 'e.g. NOR, VER')}
            ${textRow('alerts.favouriteTeams', 'Favourite teams (short names)', (a.favouriteTeams || []).join(', '), 'e.g. McLaren, Ferrari')}
        </div>
        <div class="set-section"><h4>Authentication</h4>
            ${numRow('auth.expiryWarningHours', 'Token-expiry warning (hours)', au.expiryWarningHours)}
            ${numRow('auth.expiryCheckIntervalSeconds', 'Token-check interval (seconds)', au.expiryCheckIntervalSeconds)}
        </div>`;
    }

    function setDeep(obj, path, val) {
        const parts = path.split('.');
        let cur = obj;
        for (let i = 0; i < parts.length - 1; i++) {
            cur[parts[i]] = cur[parts[i]] || {};
            cur = cur[parts[i]];
        }
        cur[parts[parts.length - 1]] = val;
    }

    const CSV_KEYS = new Set(['alerts.favouriteDrivers', 'alerts.favouriteTeams']);

    function collect(root) {
        const out = {};
        root.querySelectorAll('[data-key]').forEach((el) => {
            const key = el.dataset.key;
            let val;
            if (el.type === 'checkbox') val = el.checked;
            else if (el.type === 'number') val = el.value === '' ? null : Number(el.value);
            else if (CSV_KEYS.has(key)) val = el.value.split(',').map((x) => x.trim()).filter(Boolean);
            else val = el.value;
            if (val !== null) setDeep(out, key, val);
        });
        return out;
    }

    function ensureModal() {
        if (document.getElementById('settingsModal')) return;
        const m = document.createElement('div');
        m.id = 'settingsModal';
        m.className = 'settings-modal hidden';
        m.innerHTML =
            `<div class="settings-dialog">
                <div class="settings-head"><h3>Settings</h3><button class="settings-close" aria-label="Close">&times;</button></div>
                <div class="settings-body" id="settingsBody"></div>
                <div class="settings-foot">
                    <span class="settings-msg" id="settingsMsg"></span>
                    <button class="settings-btn settings-cancel">Cancel</button>
                    <button class="settings-btn settings-save">Save</button>
                </div>
            </div>`;
        document.body.appendChild(m);
        m.querySelector('.settings-close').addEventListener('click', close);
        m.querySelector('.settings-cancel').addEventListener('click', close);
        m.querySelector('.settings-save').addEventListener('click', save);
        m.addEventListener('click', (e) => { if (e.target === m) close(); });
    }

    async function open() {
        ensureModal();
        const s = await load();
        document.getElementById('settingsBody').innerHTML = buildForm(s || {});
        document.getElementById('settingsMsg').textContent = '';
        document.getElementById('settingsModal').classList.remove('hidden');
    }
    function close() {
        const m = document.getElementById('settingsModal');
        if (m) m.classList.add('hidden');
    }
    async function save() {
        const updates = collect(document.getElementById('settingsBody'));
        const msg = document.getElementById('settingsMsg');
        msg.textContent = 'Saving…';
        try {
            const r = await fetch(API, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates),
            });
            if (!r.ok) throw new Error(r.status);
            current = await r.json();
            window.F1_SETTINGS = current;
            msg.textContent = 'Saved.';
            setTimeout(close, 600);
        } catch (e) {
            msg.textContent = 'Save failed.';
        }
    }

    async function pickCacheDir() {
        const msg = document.getElementById('settingsMsg');
        let path;
        try {
            path = (await (await fetch('/api/v1/settings/pick-folder', { method: 'POST' })).json()).path;
        } catch (e) { path = ''; }
        if (!path) return;
        const move = window.confirm('Move existing cached files to the new location?');
        msg.textContent = move ? 'Moving cache…' : 'Saving…';
        try {
            const r = await fetch('/api/v1/settings/cache-location', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path, move }),
            });
            if (!r.ok) throw new Error(r.status);
            const d = await r.json();
            const disp = document.getElementById('set-cacheDir-display');
            if (disp) disp.value = d.cacheDir;
            msg.textContent = '';
            window.alert('Cache location updated. Please restart F1 Unleashed for the change to take effect.');
        } catch (e) {
            msg.textContent = 'Cache change failed.';
        }
    }

    window.openSettings = open;
    document.addEventListener('click', (e) => {
        if (e.target.closest('.open-settings')) { e.preventDefault(); open(); return; }
        if (e.target.closest('#pickCacheDir')) { e.preventDefault(); pickCacheDir(); }
    });

    // Case-insensitive favourite matchers for consumers (standings, alerts).
    // Drivers match by exact TLA (case-insensitive); teams match by substring
    // either way, so a short "McLaren" matches F1's "McLaren Mercedes".
    function _favs(key) {
        const a = (window.F1_SETTINGS && window.F1_SETTINGS.alerts) || {};
        return (a[key] || []).map((s) => String(s).trim().toLowerCase()).filter(Boolean);
    }
    window.f1IsFavouriteDriver = (tla) =>
        !!tla && _favs('favouriteDrivers').includes(String(tla).trim().toLowerCase());
    window.f1IsFavouriteTeam = (name) => {
        if (!name) return false;
        const n = String(name).trim().toLowerCase();
        return _favs('favouriteTeams').some((f) => n.includes(f) || f.includes(n));
    };

    // Prime F1_SETTINGS for components that read it (auto-play, favourites).
    load();
})();
