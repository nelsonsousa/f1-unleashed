// Trello 5695aUWA "Sector yellow flag: mark the track outline too, not just the
// marshal-sector centreline" — a single-sector yellow flag must highlight BOTH the
// marshal-sector centreline (#track-sectors, .sector-yellow — pre-existing, WB-27's
// investigation) AND the matching arc of the track outline (#track-outline,
// .sector-yellow-outline — new). #track-outline's per-sector <path> elements already carry
// the same data-sector numbering as #track-sectors (baked into every circuit SVG under
// static/images/tracks/), so handleYellowFlag() is extended to scope its two clear/apply
// passes to each group rather than the old wildcard `[data-sector]` selector, and to use a
// distinct class (.sector-yellow-outline) for the outline so it can carry the flag-blink
// visual convention instead of the centreline's .sector-yellow styling.
//
// Extracts handleYellowFlag() from track_map.js via its source markers (same technique as
// test_wb27_track_status_flag_scope.mjs / test_wb16_team_colors_client.mjs — track_map.js
// touches `window`/`document`/`messageBus` at module scope, so the whole file can't load
// under `node --test`).
//
// Run: node --test tests/test_5695aUWA_sector_yellow_outline.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const fullSrc = readFileSync(join(here, '..', 'static', 'js', 'components', 'tiles', 'track_map.js'), 'utf8');

const startMarker = 'function handleYellowFlag(data) {';
const startIdx = fullSrc.indexOf(startMarker);
assert.ok(startIdx !== -1, 'expected to find handleYellowFlag() in track_map.js');
// Function body ends at the first line consisting of just 4-space-indented "}" after the start.
const endMarker = '\n    }\n';
const endIdx = fullSrc.indexOf(endMarker, startIdx);
assert.ok(endIdx !== -1, 'expected to find the closing brace of handleYellowFlag()');
const block = fullSrc.slice(startIdx, endIdx + endMarker.length - 1);   // keep the closing "}"

// Fake <path> element: classList (add/remove/contains), tagged with its data-sector and
// which group (#track-sectors vs #track-outline) it belongs to.
function fakePath(sector) {
    const classes = new Set();
    return {
        sector,
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
    };
}

// Minimal fake SVG root exposing only the two compound-selector shapes handleYellowFlag()
// actually uses: "#track-sectors [data-sector]" / "#track-outline [data-sector]" (all
// sector paths in that group) and their `="N"` (single-sector) variants.
function makeFakeTrackSvg(sectorPaths, outlinePaths) {
    return {
        querySelectorAll(sel) {
            const m = sel.match(/^#track-(sectors|outline) \[data-sector(?:="(\d+)")?\]$/);
            assert.ok(m, `unexpected selector passed to querySelectorAll: ${sel}`);
            const group = m[1] === 'sectors' ? sectorPaths : outlinePaths;
            return m[2] ? group.filter(p => p.sector === m[2]) : group;
        },
    };
}

function makeHarness(sectorCount) {
    const sectorPaths = [];
    const outlinePaths = [];
    for (let i = 1; i <= sectorCount; i++) {
        sectorPaths.push(fakePath(String(i)));
        outlinePaths.push(fakePath(String(i)));
    }
    const trackSvg = makeFakeTrackSvg(sectorPaths, outlinePaths);
    const fn = new Function('state', block + '\nreturn handleYellowFlag;');
    const handleYellowFlag = fn({ trackSvg });
    return { handleYellowFlag, sectorPaths, outlinePaths };
}

test('a sector yellow flag highlights both the centreline and the matching outline arc', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['5']);

    const flaggedSector = sectorPaths.find(p => p.sector === '5');
    const flaggedOutline = outlinePaths.find(p => p.sector === '5');
    assert.equal(flaggedSector.classList.contains('sector-yellow'), true,
        'flagged sector must get .sector-yellow on the centreline (pre-existing behaviour)');
    assert.equal(flaggedOutline.classList.contains('sector-yellow-outline'), true,
        'flagged sector must ALSO get .sector-yellow-outline on the matching outline arc (5695aUWA)');

    // Outline-appropriate styling, not the centreline class — the two must not cross over.
    assert.equal(flaggedOutline.classList.contains('sector-yellow'), false,
        'the outline arc must use .sector-yellow-outline, not the centreline .sector-yellow class');
    assert.equal(flaggedSector.classList.contains('sector-yellow-outline'), false,
        'the centreline segment must not pick up the outline-only class');
});

test('unflagged sectors receive neither class, in either group', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['5']);
    for (const p of sectorPaths) {
        if (p.sector !== '5') assert.equal(p.classList.contains('sector-yellow'), false);
    }
    for (const p of outlinePaths) {
        if (p.sector !== '5') assert.equal(p.classList.contains('sector-yellow-outline'), false);
    }
});

test('multiple simultaneous sector flags all mark their centreline + outline pair', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['3', '12']);
    for (const sector of ['3', '12']) {
        assert.equal(sectorPaths.find(p => p.sector === sector).classList.contains('sector-yellow'), true);
        assert.equal(outlinePaths.find(p => p.sector === sector).classList.contains('sector-yellow-outline'), true);
    }
});

test('a later call clears the previous flag from both groups before applying the new one', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['5']);
    handleYellowFlag(['9']);

    assert.equal(sectorPaths.find(p => p.sector === '5').classList.contains('sector-yellow'), false);
    assert.equal(outlinePaths.find(p => p.sector === '5').classList.contains('sector-yellow-outline'), false);
    assert.equal(sectorPaths.find(p => p.sector === '9').classList.contains('sector-yellow'), true);
    assert.equal(outlinePaths.find(p => p.sector === '9').classList.contains('sector-yellow-outline'), true);
});

test('an empty flag list clears every sector in both groups', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['5']);
    handleYellowFlag([]);
    for (const p of [...sectorPaths, ...outlinePaths]) {
        assert.equal(p.classList.contains('sector-yellow'), false);
        assert.equal(p.classList.contains('sector-yellow-outline'), false);
    }
});

test('a non-array payload (e.g. null) clears highlights and adds none, without throwing', () => {
    const { handleYellowFlag, sectorPaths, outlinePaths } = makeHarness(20);
    handleYellowFlag(['5']);
    assert.doesNotThrow(() => handleYellowFlag(null));
    for (const p of [...sectorPaths, ...outlinePaths]) {
        assert.equal(p.classList.contains('sector-yellow'), false);
        assert.equal(p.classList.contains('sector-yellow-outline'), false);
    }
});
