// RCM O(N²)-on-seek fix (B05 pfH0yVo7): the server replays each message
// individually on a restore/seek, and the RCM/radio history arrives AFTER
// state:seek-complete — so a _restoring flag keyed on seek-complete is defeated.
// Correct fix = rAF-coalesce renderAll (ordering-independent): N messages within
// a frame collapse to ONE paint. Run: node --test tests/test_rcm_batched_restore.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
    join(here, '..', 'static', 'js', 'components', 'tiles', 'race_control.js'), 'utf8');

test('renderAll coalesces via requestAnimationFrame → renderAllNow', () => {
    assert.match(
        src,
        /function renderAll\(\)\s*\{\s*if \(_renderPending\) return;\s*_renderPending = true;\s*requestAnimationFrame\([\s\S]*?renderAllNow\(\)/,
        'renderAll must be the rAF-coalescing wrapper');
});

test('renderAllNow does the actual innerHTML build', () => {
    assert.match(src, /function renderAllNow\(\)/);
    assert.match(src, /rcm\.innerHTML/);
});

test('the defeated _restoring flag is fully removed', () => {
    assert.doesNotMatch(src, /_restoring/,
        'the seek-complete-keyed _restoring flag was defeated by frame ordering');
});
