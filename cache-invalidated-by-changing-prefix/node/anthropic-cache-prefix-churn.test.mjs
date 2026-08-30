import { test } from 'node:test';
import assert from 'node:assert/strict';
import { churnRuns, classify, gapProfile, handoff, minuteIndex, minuteKey,
         rowsByKey, totals, ttlSplit, writeShare, writes }
  from './anthropic-cache-prefix-churn.mjs';

const BASE = minuteIndex('2026-08-31T10:00Z');

const minute = (offset, { uncached = 100000, write5m = 0, write1h = 0,
                          reads = 0 } = {}) => {
  const hour = Math.floor(offset / 60);
  const rest = offset % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return { minute: `2026-08-31T${pad(10 + hour)}:${pad(rest)}Z`,
           index: BASE + offset, uncached, write5m, write1h, reads };
};

const CHURN = Array.from({ length: 120 }, (_, i) => minute(i, { write5m: 500000 }));
const SLOW = Array.from({ length: 120 },
  (_, i) => minute(i, { write5m: i % 20 === 0 ? 10000000 : 0 }));

test('a write in every adjacent minute and never a read', () => {
  const sums = totals(CHURN);
  assert.equal(sums.writes, 60000000);
  assert.equal(sums.uncached, 12000000);
  assert.equal(sums.reads, 0);
  assert.equal(sums.active, 120);
  assert.equal(Number(writeShare(CHURN[0]).toFixed(4)), 0.8333);

  const runs = churnRuns(CHURN);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].length, 120);

  const [state, detail] = classify(CHURN);
  assert.equal(state, 'prefix-churn');
  assert.match(detail, /longest run 120 adjacent minute/);
  assert.match(detail, /from 2026-08-31T10:00Z to 2026-08-31T11:59Z/);
  assert.equal(ttlSplit(sums)[0], '5m-dominant');
});

test('identical totals spaced out are a different note', () => {
  assert.equal(totals(SLOW).writes, totals(CHURN).writes);
  assert.equal(totals(SLOW).uncached, totals(CHURN).uncached);
  assert.equal(totals(SLOW).reads, 0);
  assert.equal(totals(CHURN).reads, 0);

  assert.equal(Math.max(...churnRuns(SLOW).map((r) => r.length)), 1);
  assert.equal(gapProfile(SLOW), 20);
  assert.equal(gapProfile(CHURN), 1);

  const [state, detail] = classify(SLOW);
  assert.equal(state, 'gap-driven-misses');
  assert.match(detail, /median of 20 minute\(s\) apart/);
  assert.match(handoff(state), /cache-writes-with-no-reads/);
});

test('reads anywhere hand the finding to the ratio note', () => {
  const warm = Array.from({ length: 120 }, (_, i) => minute(i, {
    write5m: i === 0 ? 500000 : 0, reads: i ? 400000 : 0 }));
  const [state, detail] = classify(warm);
  assert.equal(state, 'cache-is-read');
  assert.match(detail, /against 500000 written/);
  assert.match(handoff(state), /write-to-read ratio/);
});

test('no writes and no reads is the never switched on note', () => {
  const off = Array.from({ length: 120 }, (_, i) => minute(i));
  const [state, detail] = classify(off);
  assert.equal(state, 'caching-off');
  assert.match(detail, /no cache writes and no cache reads/);
  assert.match(handoff(state), /prompt-caching-never-used/);
  assert.equal(ttlSplit(totals(off))[0], 'no-writes');
  const readsOnly = Array.from({ length: 120 }, (_, i) => minute(i, { reads: 400000 }));
  assert.equal(classify(readsOnly)[0], 'reads-only');
});

test('a minority cached fragment is not the prefix', () => {
  const small = Array.from({ length: 120 },
    (_, i) => minute(i, { uncached: 900000, write5m: 100000 }));
  const [state, detail] = classify(small);
  assert.equal(state, 'small-cached-prefix');
  assert.match(detail, /writes are 10% of input/);
  assert.equal(handoff(state), '');
});

test('an hour long ttl makes the same run worse', () => {
  const hourly = Array.from({ length: 120 }, (_, i) => minute(i, { write1h: 500000 }));
  assert.equal(classify(hourly)[0], 'prefix-churn');
  const [ttlState, ttlDetail] = ttlSplit(totals(hourly));
  assert.equal(ttlState, '1h-dominant');
  assert.match(ttlDetail, /2x base input/);
  assert.equal(ttlSplit({ write5m: 10, write1h: 10 })[0], 'mixed');
});

test('a run crossing an hour boundary is not broken in half', () => {
  const crossing = Array.from({ length: 6 },
    (_, i) => minute(57 + i, { write5m: 500000 }));
  assert.deepEqual(crossing.slice(0, 4).map((r) => r.minute),
    ['2026-08-31T10:57Z', '2026-08-31T10:58Z', '2026-08-31T10:59Z',
     '2026-08-31T11:00Z']);
  const runs = churnRuns(crossing);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].length, 6);
  assert.equal(minuteIndex('2026-08-31T11:00Z') - minuteIndex('2026-08-31T10:59Z'), 1);
});

test('the nested cache creation object is actually read', () => {
  const buckets = Array.from({ length: 6 }, (_, i) => ({
    starting_at: `2026-08-31T10:0${i}Z`,
    results: [{ api_key_id: 'apikey_01Ab', model: 'claude-opus-5',
                uncached_input_tokens: 100000,
                cache_read_input_tokens: 0,
                cache_creation: { ephemeral_5m_input_tokens: 500000,
                                  ephemeral_1h_input_tokens: 0 } }],
  }));
  const series = rowsByKey(buckets);
  const rows = [...series.values()][0];
  assert.equal(rows.length, 6);
  assert.equal(writes(rows[0]), 500000);
  assert.equal(classify(rows, 5, 0.5, 0.01, 6)[0], 'prefix-churn');
});

test('thin and unreadable windows produce no verdict', () => {
  const thin = Array.from({ length: 4 }, (_, i) => minute(i, { write5m: 500000 }));
  assert.equal(classify(thin)[0], 'too-little-traffic');
  assert.equal(classify([])[0], 'too-little-traffic');
  assert.equal(classify(null)[0], 'too-little-traffic');
  assert.equal(writeShare({ uncached: 0, write5m: 0, write1h: 0 }), null);
  assert.equal(gapProfile([]), null);
  assert.equal(minuteKey('nonsense'), null);
  assert.equal(minuteIndex(null), null);
  assert.equal(rowsByKey([{ starting_at: 'bad', results: [] }]).size, 0);
});
