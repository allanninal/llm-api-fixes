import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, completeDays, corroborate, daily, dayKey, keyActivity,
         surfaceSplit } from './openai-project-went-quiet.mjs';

const DAYS = Array.from({ length: 14 }, (_, i) => `2026-08-${String(i + 5).padStart(2, '0')}`);
const NOW = 1787097600; // 2026-08-19T00:00:00Z

test('a project that stops is named with a date and a volume', () => {
  const series = new Map(DAYS.slice(0, 12).map((day) => [day, 4102]));
  const [state, detail] = classify(series, DAYS);
  assert.equal(state, 'went-quiet');
  assert.match(detail, /last traffic on 2026-08-16/);
  assert.match(detail, /2 complete day\(s\) ago/);
  assert.match(detail, /prior mean of 4102 request\(s\) a day/);

  const busy = new Map(DAYS.map((day) => [day, 4102]));
  assert.equal(classify(busy, DAYS)[0], 'live');
});

test('a launch is not a death read backwards', () => {
  const [state, detail] = classify(new Map([[DAYS[12], 900], [DAYS[13], 1200]]), DAYS);
  assert.equal(state, 'new-traffic');
  assert.match(detail, /first traffic in this window landed on 2026-08-17/);
});

test('the quiet states that are not findings', () => {
  assert.equal(classify(new Map(), DAYS)[0], 'never-active');
  assert.equal(classify(new Map([[DAYS[0], 4]]), DAYS)[0], 'too-little-traffic');
  assert.equal(classify(new Map([[DAYS[0], 4102]]), DAYS.slice(0, 2))[0],
               'window-too-short');
  assert.equal(classify(null, DAYS)[0], 'never-active');
});

test('today is never in the axis', () => {
  const days = completeDays(NOW, 14);
  assert.deepEqual(days, DAYS);
  assert.equal(dayKey(NOW), '2026-08-19');
  assert.ok(!days.includes(dayKey(NOW)));
  assert.equal(dayKey('not an epoch'), null);
});

test('a project absent from a bucket is a zero not a gap', () => {
  const buckets = [
    { start_time: 1786579200,
      results: [{ project_id: 'proj_busy', num_model_requests: 10 }] },
    { start_time: 1786665600, results: [] },
  ];
  const rows = daily(buckets);
  assert.deepEqual([...rows.get('proj_busy')], [['2026-08-13', 10]]);
  assert.equal(rows.get('proj_quiet'), undefined);
  const images = daily([{ start_time: 1786579200,
                          results: [{ project_id: 'p', num_images: 7 }] }]);
  assert.deepEqual([...images.get('p')], [['2026-08-13', 7]]);
  assert.equal(daily([]).size, 0);
});

test('a key still in use means something is authenticating', () => {
  const keys = [{ last_used_at: NOW - 3600 }, { last_used_at: null },
                { last_used_at: NOW - 900000 }];
  const [used, since] = keyActivity(keys, NOW);
  assert.equal(used, NOW - 3600);
  assert.equal(Number(since.toFixed(2)), 0.04);
  const [state, detail] = corroborate(since);
  assert.equal(state, 'key-still-used');
  assert.match(detail, /authenticating and not inferring/);
});

test('a key frozen with the buckets means the integration died', () => {
  const [, since] = keyActivity([{ last_used_at: NOW - 11 * 86400 }], NOW);
  const [state, detail] = corroborate(since);
  assert.equal(state, 'key-quiet-too');
  assert.match(detail, /11\.0 day\(s\) ago/);
  assert.deepEqual(keyActivity([], NOW), [null, null]);
  assert.deepEqual(keyActivity([{ last_used_at: 'never' }], NOW), [null, null]);
  assert.equal(corroborate(null)[0], 'no-key-use');
});

test('one quiet surface beside a live one is a code path', () => {
  const [quiet, live] = surfaceSplit({ completions: 'went-quiet',
                                       embeddings: 'live',
                                       images: 'never-active' });
  assert.deepEqual(quiet, ['completions']);
  assert.deepEqual(live, ['embeddings']);
  assert.deepEqual(surfaceSplit({}), [[], []]);
});
