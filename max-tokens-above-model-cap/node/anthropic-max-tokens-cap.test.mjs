import { test } from 'node:test';
import assert from 'node:assert/strict';
import { effectiveCap, parsePath, syncCap, tierSpans, verdict, windowOf }
  from './anthropic-max-tokens-cap.mjs';

const SONNET = { id: 'claude-sonnet-5', max_tokens: 128000, max_input_tokens: 1000000 };
const HAIKU = { id: 'claude-haiku-4-5-20251001', max_tokens: 64000, max_input_tokens: 200000 };

test('the same value is legal on one model and a 400 on the other', () => {
  assert.equal(verdict(128000, effectiveCap(SONNET)[0])[0], 'at-cap');
  const [state, detail] = verdict(128000, effectiveCap(HAIKU)[0]);
  assert.equal(state, 'above-cap');
  assert.match(detail, /against a cap of 64000/);
  assert.match(detail, /64000 over/);
  assert.match(detail, /400/);
});

test('the batch ceiling needs the endpoint and the header and the window', () => {
  const [cap, source] = effectiveCap(SONNET, 'batches', ['output-300k-2026-03-24']);
  assert.equal(cap, 300000);
  assert.match(source, /output-300k-2026-03-24/);
  assert.equal(effectiveCap(SONNET, 'messages', ['output-300k-2026-03-24'])[0], 128000);
  assert.equal(effectiveCap(SONNET, 'batches', [])[0], 128000);
  const [haikuCap, haikuSource] = effectiveCap(HAIKU, 'batches', ['output-300k-2026-03-24']);
  assert.equal(haikuCap, 64000);
  assert.match(haikuSource, /1M context model/);
});

test('a model object with no cap is not an unlimited one', () => {
  assert.equal(syncCap({ id: 'claude-sonnet-5' }), null);
  assert.equal(syncCap({ max_tokens: 0 }), null);
  assert.equal(syncCap({ max_tokens: '128000' }), null);
  assert.equal(syncCap(null), null);
  assert.equal(windowOf(HAIKU), 200000);
  assert.equal(windowOf({}), null);
  const [state, detail] = verdict(128000, effectiveCap({ id: 'x' })[0]);
  assert.equal(state, 'cap-unknown');
  assert.match(detail, /no ceiling could be read/);
});

test('the floor is one and it is a different finding', () => {
  assert.equal(verdict(0, 128000)[0], 'below-minimum');
  assert.equal(verdict(-1, 128000)[0], 'below-minimum');
  assert.equal(verdict(1, 128000)[0], 'within-cap');
});

test('a value sitting exactly on the ceiling is its own warning', () => {
  const [state, detail] = verdict(64000, 64000);
  assert.equal(state, 'at-cap');
  assert.match(detail, /any move to a smaller model breaks this path/);
  assert.deepEqual(verdict(16000, 64000),
    ['within-cap', 'max_tokens is 16000 of a 64000 cap (25%)']);
});

test('one number shared across two tiers is reported before it breaks', () => {
  const rows = [['reports', 'claude-opus-5', 64000, 128000],
                ['classifier', 'claude-haiku-4-5-20251001', 64000, 64000],
                ['summaries', 'claude-sonnet-5', 8000, 128000]];
  assert.deepEqual(tierSpans(rows),
    [[64000, ['claude-haiku-4-5-20251001', 'claude-opus-5']]]);
  assert.deepEqual(tierSpans(rows.slice(2)), []);
  assert.deepEqual(tierSpans([]), []);
  assert.deepEqual(tierSpans(null), []);
});

test('the shorthand argument parses model ids that contain no colon', () => {
  assert.deepEqual(parsePath('classifier=claude-haiku-4-5-20251001:64000'),
    ['classifier', { model: 'claude-haiku-4-5-20251001', max_tokens: 64000,
                     endpoint: 'messages' }]);
  assert.equal(parsePath('reports=claude-opus-5:128000')[1].max_tokens, 128000);
  assert.equal(parsePath('no-colon=claude-opus-5'), null);
  assert.equal(parsePath('claude-opus-5:128000'), null);
  assert.equal(parsePath('reports=claude-opus-5:lots'), null);
  assert.equal(parsePath(''), null);
  assert.equal(parsePath(null), null);
});
