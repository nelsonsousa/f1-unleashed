// Regression test for the map/telemetry-trace-lags-behind-standings bug
// (Trello CKlHX0s6, docs/artifacts/2026-08-19-065-timing-telemetry-lag-replay/).
//
// Commit 11b5f2e (2026-07-24, card 1MkVQjNb/479) raised POS_LAG_MS
// (track_map.js) and TEL_LAG_MS (telemetry.js) from 500 to 10000, which
// rendered the track map and telemetry trace ~9.5s behind every other
// (clock-driven) tile with no live/replay branch. `repos/live`/`repos/next`
// never carried the regression (still 500); `repos/dev`/`test`/`e2e` did.
//
// These are the ONLY two `_LAG_MS`-style per-tile render-delay constants
// anywhere in static/js (confirmed by the investigation's grep). This test
// pins both to a small buffer (500ms — enough to smooth ~3.7Hz sample
// interpolation) rather than a value large enough to be a visible lag
// against the rest of the UI.
//
// Run: node --test tests/test_ckihx0s6_tile_lag_constants.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const staticJsRoot = join(here, '..', 'static', 'js');

const trackMapSrc = readFileSync(
    join(staticJsRoot, 'components', 'tiles', 'track_map.js'), 'utf8'
);
const telemetrySrc = readFileSync(
    join(staticJsRoot, 'components', 'tiles', 'telemetry.js'), 'utf8'
);

// A "visible lag" threshold, not "must equal 500 exactly" — the point is to
// forbid a multi-second per-tile divergence from the shared clock, not to
// freeze the precise smoothing buffer forever. 2000ms is generous headroom
// above the current 500ms buffer while still catching the 10000ms regression.
const MAX_SANE_LAG_MS = 2000;

function extractLagConstant(src, name) {
    const m = src.match(new RegExp(`const\\s+${name}\\s*=\\s*(\\d+)\\s*;`));
    assert.ok(m, `expected to find \`const ${name} = <number>;\` in source`);
    return Number(m[1]);
}

test('POS_LAG_MS (track_map.js) is a small smoothing buffer, not a visible lag', () => {
    const value = extractLagConstant(trackMapSrc, 'POS_LAG_MS');
    assert.ok(
        value <= MAX_SANE_LAG_MS,
        `POS_LAG_MS = ${value}ms exceeds ${MAX_SANE_LAG_MS}ms — this renders the ` +
        `map visibly behind every other (clock-driven) tile (regression: commit ` +
        `11b5f2e set this to 10000). See CKlHX0s6 / ` +
        `docs/artifacts/2026-08-19-065-timing-telemetry-lag-replay/.`
    );
});

test('TEL_LAG_MS (telemetry.js) is a small smoothing buffer, not a visible lag', () => {
    const value = extractLagConstant(telemetrySrc, 'TEL_LAG_MS');
    assert.ok(
        value <= MAX_SANE_LAG_MS,
        `TEL_LAG_MS = ${value}ms exceeds ${MAX_SANE_LAG_MS}ms — this renders the ` +
        `telemetry trace visibly behind every other (clock-driven) tile (regression: ` +
        `commit 11b5f2e set this to 10000). See CKlHX0s6 / ` +
        `docs/artifacts/2026-08-19-065-timing-telemetry-lag-replay/.`
    );
});

test('POS_LAG_MS and TEL_LAG_MS stay in parity with each other', () => {
    const pos = extractLagConstant(trackMapSrc, 'POS_LAG_MS');
    const tel = extractLagConstant(telemetrySrc, 'TEL_LAG_MS');
    assert.equal(
        pos, tel,
        'the map and telemetry-trace smoothing buffers diverged — they should track ' +
        'the same value so neither tile drifts relative to the other or to standings'
    );
});
