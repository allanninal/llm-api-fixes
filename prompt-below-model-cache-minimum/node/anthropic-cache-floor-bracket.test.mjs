import { test } from 'node:test';
import assert from 'node:assert/strict';
import { byKey, cacheMinimum, classify, floorBracket, handoff,
         modelsCachingAnywhere, repairLines, series, splitRows }
  from './anthropic-cache-floor-bracket.mjs';

const model = (name, { uncached = 5000000, writes = 0, reads = 0 } = {}) => ({
  model: name, floor: cacheMinimum(name), uncached, writes, reads,
});

const BRACKETED = [
  model('claude-opus-5', { writes: 2000000, reads: 9000000 }),
  model('claude-sonnet-5-20260115', { writes: 1500000, reads: 7000000 }),
  model('claude-haiku-3-5'),
  model('claude-haiku-4-5-20251001'),
];

test('the bracket is the finding', () => {
  const [state, detail] = classify(BRACKETED);
  assert.equal(state, 'below-cache-minimum');
  assert.match(detail, /caching works up to a floor of 1024/);
  assert.match(detail, /stops at 2048/);
  assert.match(detail, /at least 1024 tokens and under 2048/);

  const { caching, silent } = splitRows(BRACKETED);
  assert.deepEqual(floorBracket(caching, silent), [1024, 2048]);
  assert.equal(handoff(state), '');
});

test('a dated snapshot resolves to its family floor', () => {
  assert.equal(cacheMinimum('claude-haiku-4-5-20251001'), 4096);
  assert.equal(cacheMinimum('claude-sonnet-4-5-20250929'), 1024);
  assert.equal(cacheMinimum('claude-opus-5'), 512);
  assert.equal(cacheMinimum('claude-fable-5'), 512);
  assert.equal(cacheMinimum('claude-opus-4-5-20251101'), 4096);
  assert.equal(cacheMinimum('claude-opus-4-20250514'), 1024);
  assert.equal(cacheMinimum('gpt-5.6'), null);
  assert.equal(cacheMinimum(''), null);
});

test('an unknown floor is never treated as zero', () => {
  const rows = [model('claude-future-9', { writes: 1000000, reads: 5000000 }),
                model('claude-haiku-4-5')];
  const { caching, skipped } = splitRows(rows);
  assert.deepEqual(skipped.map((r) => r.model), ['claude-future-9']);
  assert.equal(caching.length, 0);
  assert.equal(classify(rows)[0], 'single-silent-model');
});

test('a silent model under a caching floor is someone elses note', () => {
  const rows = [model('claude-opus-5'),
                model('claude-haiku-4-5', { writes: 2000000, reads: 8000000 })];
  const [state, detail] = classify(rows);
  assert.equal(state, 'silent-model-under-a-caching-floor');
  assert.match(detail, /claude-opus-5 \(floor 512\) is silent/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
  const { caching, silent } = splitRows(rows);
  assert.equal(floorBracket(caching, silent), null);
});

test('no caching at all is the never switched on note', () => {
  const rows = [model('claude-opus-5'), model('claude-haiku-4-5')];
  const [state, detail] = classify(rows);
  assert.equal(state, 'no-caching-anywhere');
  assert.match(detail, /silent on all 2 model\(s\)/);
  assert.match(handoff(state), /prompt-caching-never-used/);
});

test('one silent model is ambiguous and says so', () => {
  const [state, detail] = classify([model('claude-haiku-4-5')]);
  assert.equal(state, 'single-silent-model');
  assert.match(detail, /no second floor to bracket against/);
  assert.match(handoff(state), /prompt-caching-never-used/);
  assert.match(handoff(state), /remain open/);
});

test('a peer key caching the same model clears the model', () => {
  const peers = new Set(['claude-haiku-4-5']);
  const [state, detail] = classify([model('claude-haiku-4-5')], peers);
  assert.equal(state, 'peer-caches-same-model');
  assert.match(detail, /another key caches on the same model/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
});

test('a thin silent model is not evidence', () => {
  const rows = [model('claude-opus-5', { writes: 1000000, reads: 4000000 }),
                model('claude-haiku-4-5', { uncached: 900 })];
  const { silent, skipped } = splitRows(rows);
  assert.equal(silent.length, 0);
  assert.equal(skipped.length, 1);
  assert.equal(classify(rows)[0], 'caches-on-every-model');
});

test('the report is folded into keys and models', () => {
  const buckets = Array.from({ length: 5 }, (_, i) => ({
    starting_at: `2026-08-0${i + 1}T00:00:00Z`,
    results: [
      { api_key_id: 'apikey_01Ab', model: 'claude-opus-5',
        uncached_input_tokens: 1000000, cache_read_input_tokens: 4000000,
        cache_creation: { ephemeral_5m_input_tokens: 500000,
                          ephemeral_1h_input_tokens: 0 } },
      { api_key_id: 'apikey_01Ab', model: 'claude-haiku-4-5',
        uncached_input_tokens: 3000000, cache_read_input_tokens: 0,
        cache_creation: {} },
    ],
  }));
  const totals = series(buckets);
  assert.equal(totals.get('apikey_01Ab\tclaude-opus-5').reads, 20000000);
  assert.equal(totals.get('apikey_01Ab\tclaude-haiku-4-5').writes, 0);
  assert.deepEqual([...modelsCachingAnywhere(totals)], ['claude-opus-5']);

  const rows = byKey(totals).get('apikey_01Ab');
  assert.deepEqual(rows.map((r) => r.floor), [512, 4096]);
  assert.equal(classify(rows)[0], 'below-cache-minimum');
  assert.ok(repairLines([512, 4096]).some((l) => l.includes('4096 tokens')));
});

test('empty and unreadable input produce no verdict', () => {
  assert.equal(classify([])[0], 'too-little-traffic');
  assert.equal(classify(null)[0], 'too-little-traffic');
  assert.equal(series([]).size, 0);
  assert.equal(series([{ results: [null, 'nonsense'] }]).size, 0);
  assert.equal(byKey(new Map()).size, 0);
  assert.equal(floorBracket([], []), null);
  assert.deepEqual(repairLines(null), []);
});
