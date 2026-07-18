// B01 [P3] — race_control.js must escape the feed-supplied RCM text before it
// reaches innerHTML. Run: node --test tests/test_race_control_escape.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  join(here, '..', 'static', 'js', 'components', 'tiles', 'race_control.js'), 'utf8');

test('rcmRow escapes the race-control message (no raw ${msg.message} in the text span)', () => {
  assert.ok(src.includes('race-control-text">${escapeHtml(msg.message)}'),
    'race-control-text span must interpolate escapeHtml(msg.message)');
  assert.ok(!/race-control-text">\$\{\s*msg\.message\s*\}/.test(src),
    'raw ${msg.message} must not reach the text span');
});

test('escapeHtml (extracted from source) neutralises markup', () => {
  const m = src.match(/function escapeHtml\(s\)\s*\{[\s\S]*?\n {4}\}/);
  assert.ok(m, 'escapeHtml function found in source');
  const escapeHtml = eval('(' + m[0] + ')');
  assert.equal(escapeHtml('<img src=x onerror=alert(1)>'),
    '&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(escapeHtml(`&<>"'`), '&amp;&lt;&gt;&quot;&#39;');
});
