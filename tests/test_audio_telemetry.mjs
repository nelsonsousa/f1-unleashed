// Audio telemetry recorder (card VOPkIiAh) — pure-core unit tests.
// Run: node --test tests/test_audio_telemetry.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'static', 'js', 'audio_telemetry.js'), 'utf8');
// Load the browser-free core. The IIFE self-guards on `typeof window`, so it
// no-ops here; we return the hoisted factory.
const createAudioTelemetry = new Function(src + '\n return createAudioTelemetry;')();

// A deterministic clock so `t` is asserted exactly (no wall-clock flakiness).
function fakeClock(start) {
    let n = start;
    return () => n++;
}

test('records events with wall-clock t, type, and passed-through fields', () => {
    const tel = createAudioTelemetry({ enabled: true, now: fakeClock(1000) });
    const ev = tel.record('append', { bytes: 4096, bufferedEnd: 12.5 });
    assert.deepEqual(ev, { t: 1000, type: 'append', bytes: 4096, bufferedEnd: 12.5 });
    assert.equal(tel.size(), 1);
});

test('disabled recorder is a no-op (opt-in)', () => {
    const tel = createAudioTelemetry({ now: fakeClock(0) });   // enabled defaults false
    assert.equal(tel.isEnabled(), false);
    assert.equal(tel.record('waiting', {}), null);
    assert.equal(tel.size(), 0);
    tel.setEnabled(true);
    tel.record('waiting', {});
    assert.equal(tel.size(), 1);
});

test('ring buffer caps at capacity and drops the OLDEST (and counts drops)', () => {
    const tel = createAudioTelemetry({ enabled: true, capacity: 3, now: fakeClock(1) });
    for (let i = 0; i < 5; i++) tel.record('tick', { i });
    assert.equal(tel.size(), 3);
    assert.equal(tel.dropped(), 2);
    const out = tel.export();
    assert.deepEqual(out.map((e) => e.i), [2, 3, 4], 'keeps the newest 3, drops 0 and 1');
});

test('export returns a defensive copy (mutation-safe)', () => {
    const tel = createAudioTelemetry({ enabled: true, now: fakeClock(0) });
    tel.record('x', { v: 1 });
    const out = tel.export();
    out[0].v = 999;
    out.push({ bogus: true });
    assert.equal(tel.export()[0].v, 1, 'internal buffer unchanged');
    assert.equal(tel.size(), 1);
});

test('clear empties the buffer and resets the drop counter', () => {
    const tel = createAudioTelemetry({ enabled: true, capacity: 1, now: fakeClock(0) });
    tel.record('a', {}); tel.record('b', {});   // forces a drop
    assert.equal(tel.dropped(), 1);
    tel.clear();
    assert.equal(tel.size(), 0);
    assert.equal(tel.dropped(), 0);
});

test('toJSON emits valid JSON with count, dropped, and the events', () => {
    const tel = createAudioTelemetry({ enabled: true, capacity: 2, now: fakeClock(10) });
    tel.record('waiting', { headroom: 0.2 });
    const parsed = JSON.parse(tel.toJSON());
    assert.equal(parsed.count, 1);
    assert.equal(parsed.capacity, 2);
    assert.equal(parsed.events[0].type, 'waiting');
    assert.equal(parsed.events[0].headroom, 0.2);
    assert.equal(parsed.events[0].t, 10);
});
