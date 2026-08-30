import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cachedShare, classify, continuationRows, handoff, hourIndex, hourLabel,
         loadSplit, resumptionRows, rowsBySeries, spearman }
  from './openai-cache-key-routing-scatter.mjs';

const BASE = hourIndex('2026-08-17T00:00Z');

const LOAD = [200, 150, 120, 100, 120, 200, 400, 900, 1800, 3000, 3600, 3800,
              3900, 3800, 3600, 3000, 2400, 1800, 1200, 800, 600, 450, 350, 260];

const hour = (offset, requests, share) => {
  const tokens = requests * 2000;
  return { index: BASE + offset, hour: hourLabel(BASE + offset), requests,
           input: tokens, cached: Math.round(tokens * share) };
};

const contiguous = (shareOfLoad) => Array.from({ length: 168 },
  (_, i) => hour(i, LOAD[i % 24], shareOfLoad(LOAD[i % 24])));

const SCATTER = contiguous((load) => 0.72 - 0.00016 * load);
const FLAT = contiguous(() => 0.10);
const RISING = contiguous((load) => 0.10 + 0.00016 * load);

// Three-hour bursts five hours apart, each opening at full tilt: the busiest
// hour of every burst is also the coldest, because it follows the idle stretch.
const BURSTY = (() => {
  const shape = [[3000, 0.0], [1000, 0.7], [400, 0.7]];
  const rows = [];
  for (let burst = 0; burst < 21; burst += 1) {
    shape.forEach(([requests, share], step) => {
      rows.push(hour(burst * 8 + step, requests, share));
    });
  }
  return rows;
})();

test('the cached share falling with load is the finding', () => {
  const [quiet, busy, quietRate, busyRate] = loadSplit(SCATTER);
  assert.equal(Number(quiet.toFixed(2)), 0.69);
  assert.equal(Number(busy.toFixed(2)), 0.16);
  assert.ok(quietRate < 300 && busyRate > 3400);

  const rho = spearman(SCATTER.map((r) => r.requests),
                       SCATTER.map((r) => cachedShare([r])));
  assert.equal(Number(rho.toFixed(3)), -1);

  const [state, detail] = classify(SCATTER);
  assert.equal(state, 'load-correlated-misses');
  assert.match(detail, /68% in the quietest hours/);
  assert.match(detail, /16% in the busiest/);
  assert.match(detail, /rank correlation -1\.00/);
  assert.equal(handoff(state), '');
});

test('the same traffic with a flat share is the prefix note', () => {
  assert.deepEqual(FLAT.map((r) => r.requests), SCATTER.map((r) => r.requests));
  assert.equal(spearman(FLAT.map((r) => r.requests),
                        FLAT.map((r) => cachedShare([r]))), 0);
  const [state, detail] = classify(FLAT);
  assert.equal(state, 'flat-low-share');
  assert.match(detail, /low everywhere rather than low under load/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
});

test('a share that climbs with load is not scatter', () => {
  const [state, detail] = classify(RISING);
  assert.equal(state, 'share-rises-with-load');
  assert.match(detail, /the opposite of scatter/);
  assert.equal(handoff(state), '');
});

test('hours after a gap are excluded before correlating', () => {
  const everything = spearman(BURSTY.map((r) => r.requests),
                              BURSTY.map((r) => cachedShare([r])));
  assert.equal(Number(everything.toFixed(2)), -0.87);

  assert.equal(continuationRows(BURSTY).length, 42);
  assert.equal(resumptionRows(BURSTY).length, 21);
  assert.equal(cachedShare(continuationRows(BURSTY)), 0.7);
  assert.equal(cachedShare(resumptionRows(BURSTY)), 0);

  const [state, detail] = classify(BURSTY);
  assert.equal(state, 'cold-only-after-idle');
  assert.match(detail, /70% cached in linked hours against 0% in the 21 hour\(s\)/);
  assert.match(handoff(state), /prompt-cache-retention-left-at-default/);
});

test('no cached tokens at any load is an eligibility question', () => {
  const [state, detail] = classify(contiguous(() => 0));
  assert.equal(state, 'no-cached-tokens');
  assert.match(detail, /not one cached/);
  assert.match(handoff(state), /prompt-below-model-cache-minimum/);
});

test('a flat request rate supports no verdict', () => {
  const steady = Array.from({ length: 168 },
    (_, i) => hour(i, 1000, i % 2 ? 0.6 : 0.2));
  assert.equal(spearman(steady.map((r) => r.requests),
                        steady.map((r) => cachedShare([r]))), null);
  assert.equal(classify(steady)[0], 'load-does-not-vary');
});

test('pooled share is weighted by traffic', () => {
  const mixed = [hour(0, 10, 1.0), hour(1, 10000, 0.1)];
  assert.equal(Number(cachedShare(mixed).toFixed(4)), 0.1009);
  assert.equal(cachedShare([]), null);
  assert.equal(cachedShare([{ input: 0, cached: 0 }]), null);
});

test('the hour index survives both shapes and midnight', () => {
  assert.equal(hourIndex(1755388800), Math.floor(1755388800 / 3600));
  assert.equal(hourIndex('2026-08-17T23:00Z') + 1, hourIndex('2026-08-18T00:00Z'));
  assert.equal(hourLabel(hourIndex('2026-08-17T09:00Z')), '2026-08-17T09:00Z');
  assert.equal(hourIndex('nonsense'), null);
  assert.equal(hourIndex(null), null);
});

test('buckets are folded into project and model series', () => {
  const buckets = Array.from({ length: 168 }, (_, i) => ({
    start_time: (BASE + i) * 3600,
    results: [{ project_id: 'proj_abc123', model: 'gpt-5.6',
                num_model_requests: LOAD[i % 24],
                input_tokens: LOAD[i % 24] * 2000,
                input_cached_tokens: Math.trunc(LOAD[i % 24] * 2000
                  * (0.72 - 0.00016 * LOAD[i % 24])) }],
  }));
  const rows = rowsBySeries(buckets).get('proj_abc123\tgpt-5.6');
  assert.equal(rows.length, 168);
  assert.deepEqual(rows.map((r) => r.index), [...rows.map((r) => r.index)].sort((a, b) => a - b));
  assert.equal(classify(rows)[0], 'load-correlated-misses');
});

test('thin and unreadable windows produce no verdict', () => {
  const thin = Array.from({ length: 10 }, (_, i) => hour(i, 500, 0.5));
  assert.equal(classify(thin)[0], 'too-few-linked-hours');
  assert.equal(classify([])[0], 'too-few-linked-hours');
  assert.equal(classify(null)[0], 'too-few-linked-hours');
  assert.equal(spearman([1, 2], [1, 2]), null);
  assert.equal(spearman([1, 2, 3], null), null);
  assert.deepEqual(loadSplit([]), [null, null, null, null]);
  assert.equal(rowsBySeries([{ start_time: 'bad', results: [] }]).size, 0);
});
