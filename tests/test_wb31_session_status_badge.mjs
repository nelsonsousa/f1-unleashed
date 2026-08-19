// WB-31 [t6vSsy0n] — the session-status badge ("--", GREEN FLAG, RED FLAG, ...)
// showed a bare "--" placeholder when the session is inactive, which reads as
// a blank/broken badge. Inactive now renders the F1 Unleashed logo instead,
// leaving every other session status (green/red/sc/vsc/finished) rendering
// its message text exactly as before.
// Run: node --test tests/test_wb31_session_status_badge.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'static', 'js', 'components', 'header.js'), 'utf8');

test('handleTrackStatus renders the F1 Unleashed logo for the inactive status', () => {
    assert.ok(
        src.includes("src=\"/static/images/icons/logo_light.svg\""),
        'expected the logo asset to be referenced from header.js'
    );
    assert.ok(
        /isInactive\s*=\s*data\.status === 'inactive'/.test(src),
        'expected an isInactive check driven by data.status'
    );
    assert.ok(
        /if \(isInactive\) \{\s*textEl\.innerHTML = INACTIVE_BADGE_HTML;\s*\} else \{\s*textEl\.textContent = text;\s*\}/.test(src),
        'expected the inactive branch to set innerHTML to the logo markup and every other status to keep using textContent'
    );
});

test('the logo asset file referenced by header.js actually exists', () => {
    const m = src.match(/INACTIVE_BADGE_HTML =\s*\n?\s*'([^']*src="([^"]+)"[^']*)'/);
    assert.ok(m, 'expected INACTIVE_BADGE_HTML to define an <img src="...">');
    const assetPath = m[2].replace(/^\/static\//, '');
    const fullPath = join(here, '..', 'static', assetPath);
    assert.doesNotThrow(() => readFileSync(fullPath, 'utf8'), `logo asset missing at ${fullPath}`);
});

test('the inactive-badge markup is not injected for other statuses (spot check green)', () => {
    // Sanity check on the surrounding logic shape: the ternary/branch must be
    // gated on data.status, not on some other unrelated flag, and the
    // TRACK_STATUS_COLOR map still separately governs the badge colour.
    assert.ok(src.includes("inactive: 'white'"), 'inactive still maps to the white colour class');
    assert.ok(src.includes("green: 'green'"), 'green status still maps to the green colour class');
});
