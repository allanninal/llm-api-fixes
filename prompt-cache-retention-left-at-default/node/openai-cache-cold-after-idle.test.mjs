import { test } from 'node:test';
import assert from 'node:assert/strict';
import { binShares, cachedShare, classify, collapseBin, foregoneTokens, gapBin,
         handoff, hourIndex, hourLabel, repairLines, rowsBySeries, withGaps }
  from './openai-cache-cold-after-idle.mjs';

const BASE = hourIndex('2026-08-17T00:00Z');

const hour = (offset, share, requests = 800) => {
  const tokens = requests * 2000;
  return { index: BASE + offset, hour: hourLabel(BASE + offset), requests,
           input: tokens, cached: Math.round(tokens * share) };
};

/** A batch that runs 02:00 to 05:00 and then sleeps for twenty-one hours. */
const nightly = (resumeShare = 0.0, warmShare = 0.75) => {
  const rows = [];
  for (let day = 0; day < 14; day += 1) {
    for (let step = 0; step < 3; step += 1) {
      rows.push(hour(day * 24 + 2 + step, step === 0 ? resumeShare : warmShare));
    }
  }
  return rows;
};

/** Two hours on, one hour off, for a fortnight. Every gap is a single hour. */
const twoOnOneOff = (resumeShare = 0.0, warmShare = 0.70) => {
  const rows = [];
  for (let pair = 0; pair < 112; pair += 1) {
    rows.push(hour(pair * 3, resumeShare));
    rows.push(hour(pair * 3 + 1, warmShare));
  }
  return rows;
};

const NIGHTLY = nightly();
const HOURLY_GAPS = twoOnOneOff();
const CONTINUOUS = Array.from({ length: 336 }, (_, i) => hour(i, 0.60));

test('the share against gap length is the finding', () => {
  const annotated = withGaps(NIGHTLY);
  assert.equal(annotated.length, 41);
  assert.deepEqual([...new Set(annotated.map((r) => r.requests))], [800]);

  const bands = binShares(annotated);
  assert.equal(bands.continuous.hours, 28);
  assert.equal(bands.continuous.share, 0.75);
  assert.equal(bands['6-23h'].hours, 13);
  assert.equal(bands['6-23h'].share, 0);
  assert.equal(collapseBin(bands), '6-23h');

  const [state, detail] = classify(NIGHTLY);
  assert.equal(state, 'cold-after-idle');
  assert.match(detail, /75% cached in continuously busy hours/);
  assert.match(detail, /0% in the 13 hour\(s\) that resume after a gap of 6-23h/);
  assert.equal(handoff(state), '');
});

test('the shortest collapsed band is the one reported', () => {
  const bands = binShares(withGaps(HOURLY_GAPS));
  assert.equal(bands['1h'].hours, 111);
  assert.equal(bands['1h'].share, 0);
  assert.equal(collapseBin(bands), '1h');

  const [state, detail] = classify(HOURLY_GAPS);
  assert.equal(state, 'cold-after-idle');
  assert.match(detail, /gap of 1h/);
  assert.match(repairLines('1h', 0)[0], /a single idle hour is already enough/);
  assert.match(repairLines('24h+', 0)[0], /24h retention option/);
});

test('a series with no gaps is someone elses note', () => {
  const [state, detail] = classify(CONTINUOUS);
  assert.equal(state, 'never-idle');
  assert.match(detail, /only 0 of them resume after a gap/);
  assert.match(handoff(state), /prompt-cache-key-not-set/);
});

test('cold in the busy hours too is not eviction', () => {
  const [state, detail] = classify(nightly(0.0, 0.0));
  assert.equal(state, 'cold-everywhere');
  assert.match(detail, /0% cached even in continuously busy hours/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
  assert.match(handoff(state), /prompt-below-model-cache-minimum/);
});

test('a weak warm baseline refuses the finding', () => {
  const [state, detail] = classify(nightly(0.0, 0.10));
  assert.equal(state, 'warm-baseline-too-weak');
  assert.match(detail, /barely caching at the best of times/);
});

test('a batch that resumes warm is not a finding', () => {
  const [state, detail] = classify(nightly(0.55));
  assert.equal(state, 'warm-after-idle');
  assert.match(detail, /no gap band has collapsed/);
});

test('the first hour of the window is dropped not guessed', () => {
  const rows = [hour(0, 0.0), hour(1, 0.9), hour(9, 0.0), hour(10, 0.9)];
  const annotated = withGaps(rows);
  assert.deepEqual(annotated.map((r) => r.gap), [0, 7, 0]);
  assert.equal(annotated.length, rows.length - 1);
  assert.deepEqual(withGaps([hour(0, 0.5)]), []);
  assert.deepEqual(withGaps([]), []);
});

test('the gap bands line up with the repairs', () => {
  assert.equal(gapBin(0), 'continuous');
  assert.equal(gapBin(1), '1h');
  assert.equal(gapBin(2), '2-5h');
  assert.equal(gapBin(5), '2-5h');
  assert.equal(gapBin(6), '6-23h');
  assert.equal(gapBin(23), '6-23h');
  assert.equal(gapBin(24), '24h+');
  assert.equal(gapBin(500), '24h+');
  const thin = { '1h': { hours: 1, input: 100, cached: 0, share: 0 },
                 '24h+': { hours: 40, input: 100, cached: 0, share: 0 } };
  assert.equal(collapseBin(thin), '24h+');
});

test('the foregone tokens are priced at the workloads own warm rate', () => {
  const bands = binShares(withGaps(NIGHTLY));
  assert.equal(bands['6-23h'].input, 13 * 800 * 2000);
  assert.equal(foregoneTokens(bands, 0.75), 15600000);
  assert.equal(foregoneTokens(bands, null), 0);
  assert.equal(foregoneTokens({}, 0.75), 0);
});

test('buckets are folded and idle hours never become rows', () => {
  const buckets = [];
  for (let day = 0; day < 14; day += 1) {
    for (let step = 0; step < 3; step += 1) {
      buckets.push({
        start_time: (BASE + day * 24 + 2 + step) * 3600,
        results: [{ project_id: 'proj_abc123', model: 'gpt-5.6',
                    num_model_requests: 800, input_tokens: 1600000,
                    input_cached_tokens: step === 0 ? 0 : 1200000 }],
      });
    }
  }
  const rows = rowsBySeries(buckets).get('proj_abc123\tgpt-5.6');
  assert.equal(rows.length, 42);
  assert.equal(cachedShare(rows), 0.5);
  assert.equal(classify(rows)[0], 'cold-after-idle');
});

test('thin and unreadable windows produce no verdict', () => {
  assert.equal(classify(Array.from({ length: 10 }, (_, i) => hour(i, 0.5)))[0],
    'too-few-hours');
  assert.equal(classify([])[0], 'too-few-hours');
  assert.equal(classify(null)[0], 'too-few-hours');
  assert.equal(cachedShare([]), null);
  assert.deepEqual(binShares([]), {});
  assert.equal(collapseBin({}), null);
  assert.equal(hourIndex('nonsense'), null);
  assert.equal(rowsBySeries([{ start_time: 'bad', results: [] }]).size, 0);
});
