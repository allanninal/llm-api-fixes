import { test } from 'node:test';
import assert from 'node:assert/strict';
import { belowPublishedStart, cacheCreation, groupForModel, peak, rampFactors,
         repairLines, series, share, uncachedInput, verdict }
  from './anthropic-ramp-acceleration.mjs';

const LIMITS = { requests_per_minute: 4000, input_tokens_per_minute: 10000000,
                 output_tokens_per_minute: 2000000 };

const pad = (n) => String(n).padStart(2, '0');

const bucket = (minute, { model = 'claude-opus-5', uncached = 0, out = 0, read = 0,
                          create5m = 0, create1h = 0 } = {}) => ({
  starting_at: `2026-08-31T09:${pad(minute)}:00Z`,
  ending_at: `2026-08-31T09:${pad(minute + 1)}:00Z`,
  results: [{ model, uncached_input_tokens: uncached, output_tokens: out,
              cache_read_input_tokens: read,
              cache_creation: { ephemeral_5m_input_tokens: create5m,
                                ephemeral_1h_input_tokens: create1h } }],
});

const page = (buckets) => [{ data: buckets, has_more: false, next_page: null }];

test('a steep step under a low ceiling is the finding', () => {
  const quiet = [0, 1, 2, 3].map((m) => bucket(m, { uncached: 130000, out: 14000 }));
  const rows = series(page([...quiet, bucket(4, { uncached: 1940000, out: 140000 })]));
  const [state, detail, facts] = verdict(rows['claude-opus-5'], LIMITS, 'claude-opus-5');
  assert.equal(state, 'acceleration-suspect');
  assert.match(detail, /step between adjacent minutes/);
  assert.deepEqual(facts.peakIn, ['2026-08-31T09:04:00Z', 1940000]);
  assert.ok(facts.inShare > 0.19 && facts.inShare < 0.20);
  assert.equal(Number(facts.ramps[0][2].toFixed(1)), 14.9);
  assert.ok(repairLines(state, facts).some((l) => l.includes('ramp gradually')));
  assert.ok(repairLines(state, facts).some((l) => l.includes('1 per second')));
});

test('the same ramp against a saturated limiter is the other note', () => {
  const quiet = [0, 1, 2, 3].map((m) => bucket(m, { out: 120000 }));
  const rows = series(page([...quiet, bucket(4, { out: 1870000 })]));
  const [state, detail] = verdict(rows['claude-opus-5'], LIMITS, 'claude-opus-5');
  assert.equal(state, 'limiter-saturated');
  assert.match(detail, /output limiter note, not this one/);
  assert.ok(repairLines(state).some((l) => l.includes('really is the headline number')));
});

test('input is summed the way the limiter counts it', () => {
  const result = { uncached_input_tokens: 1000, cache_read_input_tokens: 900000,
                   cache_creation: { ephemeral_5m_input_tokens: 400,
                                     ephemeral_1h_input_tokens: 600 } };
  assert.equal(cacheCreation(result), 1000);
  assert.equal(uncachedInput(result), 2000);
  assert.equal(uncachedInput({}), 0);
  assert.equal(uncachedInput(null), 0);
  const rows = series(page([bucket(0, { uncached: 1000, read: 900000,
                                        create5m: 400, create1h: 600 })]));
  assert.equal(rows['claude-opus-5'][0][1], 2000);
  assert.equal(rows['claude-opus-5'][0][3], 900000);
});

test('a ramp off a trivial base is not a ramp', () => {
  assert.deepEqual(rampFactors([['09:00', 12, 0, 0], ['09:01', 900, 0, 0]], 1), []);
  const big = [['09:00', 100000, 0, 0], ['09:01', 400000, 0, 0],
               ['09:02', 200000, 0, 0]];
  const factors = rampFactors(big, 1);
  assert.equal(factors.length, 1);
  assert.equal(factors[0][2], 4);
  assert.deepEqual(peak(big, 1), ['09:01', 400000]);
  assert.deepEqual(peak([], 1), ['', 0]);
  assert.equal(share(10, 0), null);
  assert.equal(share(10, null), null);
});

test('a model resolves to its group by exact membership', () => {
  const groups = [
    { group_type: 'model_group', models: ['claude-opus-4-5', 'claude-opus-4-8'],
      limits: [{ type: 'input_tokens_per_minute', value: 10000000 }] },
    { group_type: 'batch', models: null,
      limits: [{ type: 'enqueued_batch_requests', value: 500000 }] },
  ];
  assert.deepEqual(groupForModel(groups, 'claude-opus-4-8'),
                   { input_tokens_per_minute: 10000000 });
  assert.deepEqual(groupForModel(groups, 'claude-opus-5'), {});
  assert.deepEqual(groupForModel(null, 'claude-opus-5'), {});
});

test('configured limits under the published Start tier are reported', () => {
  assert.deepEqual(belowPublishedStart('claude-fable-5',
                                       { input_tokens_per_minute: 250000 }),
                   [['input_tokens_per_minute', 250000, 500000]]);
  assert.deepEqual(belowPublishedStart('claude-opus-5',
                                       { input_tokens_per_minute: 10000000 }), []);
  assert.deepEqual(belowPublishedStart('claude-opus-5', {}), []);
  assert.ok(repairLines('below-published-start').some((l) => l.includes('Evaluation tier')));
});

test('an empty window is not a finding', () => {
  const [state, detail] = verdict([], LIMITS, 'claude-opus-5');
  assert.equal(state, 'no-traffic');
  assert.match(detail, /no usage/);
  assert.equal(verdict(null, null, null)[0], 'no-traffic');
  assert.deepEqual(series(null), {});
  assert.deepEqual(repairLines('steady'), []);
  const steady = series(page([0, 1, 2].map((m) => bucket(m, { uncached: 100000 }))));
  assert.equal(verdict(steady['claude-opus-5'], LIMITS, 'claude-opus-5')[0], 'steady');
});
