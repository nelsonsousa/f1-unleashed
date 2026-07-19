// RCM O(N²)-on-seek fix (card m1lGcrA8): the server replays each message
// individually on a restore/seek; renderAll must be batched (suppressed while
// restoring) and painted once on state:seek-complete, not rebuilt per message.
// Run: node --test tests/test_rcm_batched_restore.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
    join(here, '..', 'static', 'js', 'components', 'tiles', 'race_control.js'), 'utf8');

test('renderAll bails while restoring (batched, not per-message)', () => {
    assert.match(src, /function renderAll\(\)\s*\{\s*if \(_restoring\) return;/,
        'renderAll must early-return when _restoring');
});

test('state:reset opens the batch (_restoring = true)', () => {
    assert.match(src, /'state:reset'[\s\S]{0,120}_restoring = true/);
});

test('state:seek-complete closes the batch and repaints once', () => {
    assert.match(src, /'state:seek-complete'[\s\S]{0,80}_restoring = false;\s*renderAll\(\)/);
});
