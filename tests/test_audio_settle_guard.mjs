// Single-seek settle guard (skip-stutter fix): syncAudio must NOT issue a
// corrective drift-seek while the data clock is still settling after a seek —
// that back-and-forth is the stutter. _shouldCorrectAudioDrift gates it on both
// the 0.5s deadband AND being past the settle window.
// Run: node --test tests/test_audio_settle_guard.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'static', 'js', 'components', 'header.js'), 'utf8');
const m = src.match(/function _shouldCorrectAudioDrift\([^)]*\)\s*\{[\s\S]*?\n {4}\}/);
assert.ok(m, '_shouldCorrectAudioDrift found in header.js');
const shouldCorrect = eval('(' + m[0] + ')');

test('within the settle window, a big drift is SUPPRESSED (the stutter fix)', () => {
    // now=1000ms < settleUntil=2000ms → do not seek, even at 9.6s drift
    assert.equal(shouldCorrect(9.6, 1000, 2000), false);
});

test('after the settle window, a drift past the deadband is corrected', () => {
    assert.equal(shouldCorrect(9.6, 2500, 2000), true);
    assert.equal(shouldCorrect(-3.1, 2500, 2000), true);   // negative drift too
});

test('sub-deadband drift is never corrected (even when settled)', () => {
    assert.equal(shouldCorrect(0.3, 5000, 0), false);
    assert.equal(shouldCorrect(-0.4, 5000, 0), false);
});

test('just over the 0.5s deadband, once settled, corrects', () => {
    assert.equal(shouldCorrect(0.6, 5000, 0), true);
});
