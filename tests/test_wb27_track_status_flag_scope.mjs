// WB-27 "Track decoration on SC/VSC/Red/Green" (Trello K2FTTDqg) — the track-status
// flag colouring (SC/VSC/Red/Green) must recolour only the track outline, never the
// marshal-sector centreline group (#track-sectors, the per-sector paths used for the
// separate yellow-flag sector highlight). Extracts clearTrackColour()/flashTrack() from
// track_map.js via their source markers (same technique as
// test_wb16_team_colors_client.mjs — base.js/track_map.js touch `window`/`document`/
// `messageBus` at module scope, so the whole file can't load under `node --test`) and
// exercises them against a stubbed `state.trackSvg` exposing both groups, so a
// regression that re-adds `#track-sectors` to the flashed/cleared element set is caught
// even though nothing here touches a real DOM.
//
// Run: node --test tests/test_wb27_track_status_flag_scope.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const fullSrc = readFileSync(join(here, '..', 'static', 'js', 'components', 'tiles', 'track_map.js'), 'utf8');

const startMarker = 'let _flashGen = 0;';
const endMarker = '\n    // =========================================================================\n    // Handlers';
const startIdx = fullSrc.indexOf(startMarker);
const endIdx = fullSrc.indexOf(endMarker);
assert.ok(startIdx !== -1, 'expected to find the _flashGen block start marker in track_map.js');
assert.ok(endIdx !== -1, 'expected to find the block end marker (start of Handlers section) in track_map.js');
const block = fullSrc.slice(startIdx, endIdx);

// Minimal fake SVG element: classList (add/remove/contains) + style (setProperty/getPropertyValue).
function fakeEl() {
    const classes = new Set();
    const props = {};
    return {
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
        style: {
            setProperty: (k, v) => { props[k] = v; },
            getPropertyValue: (k) => props[k] || '',
        },
    };
}

function makeHarness() {
    const outline = fakeEl();
    const sectors = fakeEl();
    const trackSvg = {
        querySelector: (sel) => (sel === '#track-outline' ? outline : sel === '#track-sectors' ? sectors : null),
    };
    const fn = new Function(
        'state',
        block + '\nreturn { clearTrackColour, flashTrack };'
    );
    const harness = fn({ trackSvg });
    return { ...harness, outline, sectors };
}

test('flashTrack (solid hold, no pulses) colours the outline but not the marshal sectors', () => {
    const { flashTrack, outline, sectors } = makeHarness();
    // pulses=0, holdSolid=true -> the solid colour is applied synchronously via `on()`,
    // no setTimeout involved, so this is deterministic without faking timers.
    flashTrack('#e10600', 0, 500, 500, true);
    assert.equal(outline.classList.contains('flag-blink'), true, 'track outline must be flagged');
    assert.equal(outline.style.getPropertyValue('--flag-color'), '#e10600');
    assert.equal(sectors.classList.contains('flag-blink'), false,
        'marshal-sector group (#track-sectors) must NOT be recoloured by track-status flags (WB-27)');
});

test('clearTrackColour only clears the outline, and never touched the sectors in the first place', () => {
    const { clearTrackColour, flashTrack, outline, sectors } = makeHarness();
    flashTrack('#ffd700', 0, 500, 500, true);
    assert.equal(outline.classList.contains('flag-blink'), true);
    clearTrackColour();
    assert.equal(outline.classList.contains('flag-blink'), false, 'clearTrackColour must clear the outline');
    assert.equal(sectors.classList.contains('flag-blink'), false, 'sectors must remain unflagged throughout');
});
