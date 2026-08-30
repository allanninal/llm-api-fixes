import { test } from 'node:test';
import assert from 'node:assert/strict';
import { flexByHour, flexGaps, hoursActive, median, neverServed, repairLines,
         stamp, tierRows, tiersForModel, totalsByTier, verdict }
  from './openai-flex-tier-served.mjs';

const HOUR = 3600;
const BASE = Math.floor(1787000000 / HOUR) * HOUR;

const result = (model, tier, requests) => ({
  object: 'organization.usage.completions.result',
  input_tokens: requests * 800, output_tokens: 0,
  num_model_requests: requests, project_id: null,
  model, batch: false, service_tier: tier,
});

const week = (flexPerHour, otherPerHour, hours = 24, model = 'gpt-5.6') => {
  const data = [];
  for (let i = 0; i < hours; i += 1) {
    const results = [];
    const flex = flexPerHour(i);
    const other = otherPerHour(i);
    if (flex) results.push(result(model, 'flex', flex));
    if (other) results.push(result(model, 'default', other));
    data.push({ object: 'bucket', start_time: BASE + i * HOUR,
                end_time: BASE + (i + 1) * HOUR, results });
  }
  return [{ object: 'page', data, has_more: false, next_page: null }];
};

test('hours where flex collapsed while other tiers kept serving', () => {
  const dead = new Set([5, 11, 19]);
  const rows = tierRows(week((i) => (dead.has(i) ? 0 : 2000), () => 8000));
  const gaps = flexGaps(flexByHour(rows, 'gpt-5.6'), hoursActive(rows));
  assert.deepEqual(gaps.map((g) => g[0]),
                   [...dead].sort((a, b) => a - b).map((h) => BASE + h * HOUR));
  assert.equal(gaps[0][1], 0);
  assert.equal(gaps[0][2], 8000);
  assert.equal(gaps[0][3], 2000);
  const [state, detail] = verdict('gpt-5.6', flexByHour(rows, 'gpt-5.6'), gaps,
                                  tiersForModel(rows, 'gpt-5.6'), ['gpt-5.6']);
  assert.equal(state, 'flex-shortfall');
  assert.match(detail, /3 hour\(s\)/);
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('Resource Unavailable')));
  assert.ok(lines.some((l) => l.includes('15 minutes')));
  assert.ok(stamp(BASE).endsWith(':00Z'));
});

test('a quiet night is not a capacity failure', () => {
  const dead = new Set([5, 11, 19]);
  const rows = tierRows(week((i) => (dead.has(i) ? 0 : 2000),
                             (i) => (dead.has(i) ? 0 : 8000)));
  assert.deepEqual(flexGaps(flexByHour(rows, 'gpt-5.6'), hoursActive(rows)), []);
  const [state] = verdict('gpt-5.6', flexByHour(rows, 'gpt-5.6'), [],
                          tiersForModel(rows, 'gpt-5.6'), ['gpt-5.6']);
  assert.equal(state, 'flex-served');
});

test('a model configured for flex that never gets it', () => {
  const rows = tierRows(week(() => 0, () => 1717));
  const tiers = tiersForModel(rows, 'gpt-5.6');
  assert.deepEqual(tiers, { default: 41208 });
  const [state, detail] = verdict('gpt-5.6', {}, [], tiers, ['gpt-5.6']);
  assert.equal(state, 'flex-never-served');
  assert.match(detail, /41,208 on other tiers/);
  assert.ok(repairLines(state).some((l) => l.includes('rewrites request bodies')));
  assert.equal(verdict('gpt-5.6', {}, [], tiers, [])[0], 'no-flex-usage');
  assert.equal(neverServed(rows, ['gpt-5.6'])[0][0], 'gpt-5.6');
  assert.deepEqual(neverServed(rows, []), []);
  assert.deepEqual(neverServed(rows, ['never-called-model']), []);
});

test('too little flex history declines rather than grading', () => {
  const rows = tierRows(week((i) => (i < 4 ? 2000 : 0), () => 8000));
  const flexHours = flexByHour(rows, 'gpt-5.6');
  assert.deepEqual(flexGaps(flexHours, hoursActive(rows)), []);
  const [state, detail] = verdict('gpt-5.6', flexHours, [],
                                  tiersForModel(rows, 'gpt-5.6'), ['gpt-5.6']);
  assert.equal(state, 'too-little-history');
  assert.match(detail, /not enough to take a median/);
  assert.ok(repairLines(state).some((l) => l.includes('not a clean bill of health')));
});

test('the median is a median and not a mean', () => {
  const values = [2000, 2000, 2000, 2000, 2000, 100000];
  assert.equal(median(values), 2000);
  assert.ok(values.reduce((a, b) => a + b, 0) / values.length > 12000);
  assert.equal(median([]), 0);
  assert.equal(median([5]), 5);
  assert.equal(median([1, 3]), 2);
});

test('the fold keeps absent hours absent', () => {
  const rows = tierRows(week((i) => (i % 2 ? 0 : 100), () => 50));
  const flexHours = flexByHour(rows, 'gpt-5.6');
  assert.equal(Object.keys(flexHours).length, 12);
  assert.ok(Object.values(flexHours).every((v) => v === 100));
  assert.equal(Object.keys(hoursActive(rows)).length, 24);
  assert.deepEqual(totalsByTier(rows), { flex: 1200, default: 1200 });
  assert.deepEqual(tierRows(null), {});
  assert.deepEqual(totalsByTier(null), {});
  assert.deepEqual(hoursActive(null), {});
  assert.deepEqual(flexByHour(null, 'x'), {});
  assert.equal(verdict('x', {}, [], {}, [])[0], 'no-flex-usage');
  assert.deepEqual(repairLines('flex-served'), []);
});
