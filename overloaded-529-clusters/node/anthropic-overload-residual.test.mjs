import { test } from 'node:test';
import assert from 'node:assert/strict';
import { attemptsByMinute, baselineTokensPerAttempt, classify, clusters,
         excessMinutes, minuteIndex, minuteKey, residualRows, tiersSeen,
         tokensByMinute } from './anthropic-overload-residual.mjs';

const minute = (n) => `2026-08-30T14:${String(n).padStart(2, '0')}Z`;

// Ten minutes at 600 attempts each; minutes 4, 5 and 6 did a fifth of the work.
const ATTEMPTS = new Map(Array.from({ length: 10 }, (_, n) => [minute(n), 600]));
const TOKENS = new Map(Array.from({ length: 10 },
  (_, n) => [minute(n), [4, 5, 6].includes(n) ? 600000 : 3000000]));

test('three adjacent bad minutes are one overload cluster', () => {
  const baseline = baselineTokensPerAttempt(TOKENS, ATTEMPTS);
  // The median survives the outage; a mean over the same data is 3800.
  assert.equal(baseline, 5000);

  const rows = residualRows(TOKENS, ATTEMPTS, baseline);
  assert.equal(rows.length, 10);
  const bad = rows.filter((r) => r.share > 0.5);
  assert.deepEqual(bad.map((r) => r.minute), [minute(4), minute(5), minute(6)]);
  assert.equal(Math.round(bad[0].residual), 480);

  const runs = clusters(rows);
  assert.equal(runs.length, 1);
  const [state, detail] = classify(runs[0]);
  assert.equal(state, 'overload-cluster');
  assert.match(detail, /2026-08-30T14:04Z through 2026-08-30T14:06Z/);
  assert.match(detail, /1800 attempt\(s\) over 3 minute\(s\)/);
  assert.match(detail, /about 1440 of them produced no billed tokens \(80%\)/);
});

test('one bad minute on its own is bucket arithmetic', () => {
  const tokens = new Map([...ATTEMPTS.keys()].map((k) => [k, 3000000]));
  tokens.set(minute(4), 600000);
  const rows = residualRows(tokens, ATTEMPTS,
                            baselineTokensPerAttempt(tokens, ATTEMPTS));
  const runs = clusters(rows);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].length, 1);
  const [state, detail] = classify(runs[0]);
  assert.equal(state, 'single-minute-dip');
  assert.match(detail, /straddled a bucket boundary/);
});

test('minutes that are not adjacent do not become one cluster', () => {
  const tokens = new Map([...ATTEMPTS.keys()].map((k) => [k, 3000000]));
  for (const n of [1, 5, 9]) tokens.set(minute(n), 100000);
  const rows = residualRows(tokens, ATTEMPTS,
                            baselineTokensPerAttempt(tokens, ATTEMPTS));
  assert.deepEqual(clusters(rows).map((run) => run.length), [1, 1, 1]);
});

test('the two clocks are normalised to the same minute', () => {
  for (const stamp of ['2026-08-30T14:03:27Z', '2026-08-30T14:03Z',
                       '2026-08-30 14:03:00+00:00', '2026-08-30T14:03:59.512Z']) {
    assert.equal(minuteKey(stamp), '2026-08-30T14:03Z');
  }
  assert.equal(minuteKey(1788098580), '2026-08-30T14:03Z');
  assert.equal(minuteKey('last tuesday'), null);
  assert.equal(minuteKey(''), null);
  assert.equal(minuteKey(null), null);
  assert.equal(minuteKey(true), null);
  assert.equal(minuteIndex('2026-08-30T15:00Z') - minuteIndex('2026-08-30T14:59Z'), 1);
});

test('the nested cache creation object is counted as work', () => {
  const buckets = [{ starting_at: '2026-08-30T14:03:00Z',
    results: [{ uncached_input_tokens: 10, cache_read_input_tokens: 20,
                output_tokens: 5, service_tier: 'standard',
                cache_creation: { ephemeral_5m_input_tokens: 100,
                                  ephemeral_1h_input_tokens: 65 } }] }];
  assert.deepEqual([...tokensByMinute(buckets)], [['2026-08-30T14:03Z', 200]]);
  assert.deepEqual([...tiersSeen(buckets)], ['standard']);
  assert.equal(tokensByMinute([]).size, 0);
});

test('an attempt file is read leniently and bad keys are dropped', () => {
  assert.deepEqual([...attemptsByMinute({ '2026-08-30T14:03:00Z': 900 })],
                   [['2026-08-30T14:03Z', 900]]);
  assert.deepEqual([...attemptsByMinute({ '2026-08-30T14:03Z': { attempts: 900 } })],
                   [['2026-08-30T14:03Z', 900]]);
  assert.equal(attemptsByMinute({ whenever: 900 }).size, 0);
  assert.equal(attemptsByMinute(null).size, 0);
});

test('more work than the attempts explain is the other note', () => {
  const attempts = new Map(Array.from({ length: 10 }, (_, n) => [minute(n), 100]));
  const tokens = new Map(Array.from({ length: 10 }, (_, n) => [minute(n), 500000]));
  tokens.set(minute(3), 2000000);
  const rows = residualRows(tokens, attempts,
                            baselineTokensPerAttempt(tokens, attempts));
  assert.deepEqual(excessMinutes(rows), [minute(3)]);
  assert.deepEqual(clusters(rows), []);
});

test('too little overlap produces no baseline rather than a guess', () => {
  assert.equal(baselineTokensPerAttempt(new Map(), new Map()), null);
  assert.equal(baselineTokensPerAttempt(new Map([[minute(0), 5000]]),
                                        new Map([[minute(0), 1]])), null);
  assert.equal(baselineTokensPerAttempt(new Map(), ATTEMPTS), null);
  assert.deepEqual(residualRows(TOKENS, ATTEMPTS, null), []);
  assert.equal(classify([])[0], 'no-cluster');
});
