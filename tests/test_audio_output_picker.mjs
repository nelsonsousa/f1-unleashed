// Audio output picker (debug) — pure device-filter unit tests.
// Run: node --test tests/test_audio_output_picker.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'static', 'js', 'audio_output_picker.js'), 'utf8');
const _audioOutputs = new Function(src + '\n return _audioOutputs;')();

test('keeps only audiooutput devices', () => {
    const devs = [
        { kind: 'audioinput', deviceId: 'mic1', label: 'Mic' },
        { kind: 'audiooutput', deviceId: 'spk1', label: 'Speakers' },
        { kind: 'videoinput', deviceId: 'cam1', label: 'Cam' },
        { kind: 'audiooutput', deviceId: 'bh1', label: 'BlackHole 2ch' },
    ];
    const out = _audioOutputs(devs);
    assert.deepEqual(out.map((o) => o.deviceId), ['spk1', 'bh1']);
    assert.deepEqual(out.map((o) => o.label), ['Speakers', 'BlackHole 2ch']);
});

test('falls back to an id-based label when the label is empty', () => {
    const out = _audioOutputs([{ kind: 'audiooutput', deviceId: 'abcdef1234567890', label: '' }]);
    assert.equal(out.length, 1);
    assert.match(out[0].label, /^Output 1 · abcdef12/);
});

test('handles empty / missing input safely', () => {
    assert.deepEqual(_audioOutputs([]), []);
    assert.deepEqual(_audioOutputs(undefined), []);
});
