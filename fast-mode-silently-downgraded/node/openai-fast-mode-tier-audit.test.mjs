import { test } from 'node:test';
import assert from 'node:assert/strict';
import { overrides, splitSpend, tierOf, verdict }
  from './openai-fast-mode-tier-audit.mjs';

function cost({ project = 'proj_a', lineItem = 'gpt-5.6-sol, input',
                value = 0 } = {}) {
  return { project_id: project, line_item: lineItem,
           amount: { value, currency: 'usd' } };
}

function buckets(...results) {
  return [{ start_time: 0, end_time: 86400, results }];
}

test('configured fast with standard spend is a downgrade', () => {
  const [state, detail] = verdict('fast', 0, 420);
  assert.equal(state, 'downgraded');
  assert.match(detail, /not one dollar/);
  assert.match(detail, /default tier/);
});

test('configured standard with premium spend is the opposite finding', () => {
  const [state, detail] = verdict('standard', 300, 100);
  assert.equal(state, 'unrequested-premium');
  assert.match(detail, /a code path is sending the tier/);
  assert.match(detail, /2\.0x/);
});

test('a delivered premium is not reported as a failure', () => {
  const [state, detail] = verdict('fast', 380, 20);
  assert.equal(state, 'premium-delivered');
  assert.match(detail, /95%/);
});

test('a partial downgrade is its own state', () => {
  const [state, detail] = verdict('fast', 100, 300);
  assert.equal(state, 'partly-downgraded');
  assert.match(detail, /only 25%/);
});

test('a missing tier field is never read as standard', () => {
  assert.equal(tierOf({ id: 'proj_a', name: 'web' }), null);
  assert.equal(tierOf({ id: 'proj_a', service_tier: '  Fast ' }), 'fast');
  assert.equal(tierOf({ id: 'proj_a', settings: { service_tier: 'priority' } }),
               'priority');
  assert.equal(tierOf({ id: 'proj_a', settings: 'fast' }), null);
  assert.equal(verdict(null, 0, 99)[0], 'unknown-tier');
  assert.equal(verdict(null, 50, 49)[0], 'unknown-tier-premium');
});

test('a project with no spend is not evidence of anything', () => {
  assert.equal(verdict('fast', 0, 0)[0], 'no-spend');
  assert.equal(verdict('standard', 0.2, 0.1)[0], 'no-spend');
});

test('premium line items are matched by label and the labels come back', () => {
  const rows = buckets(
    cost({ lineItem: 'gpt-5.6-sol, input', value: 100 }),
    cost({ lineItem: 'gpt-5.6-sol, input (fast)', value: 40 }),
    cost({ lineItem: 'gpt-5.6-sol, priority output', value: 10 }),
    cost({ project: 'proj_b', lineItem: 'gpt-5.6-sol, input (fast)', value: 999 }),
  );
  const [premium, standard, labels] = splitSpend(rows, 'proj_a');
  assert.equal(premium, 50);
  assert.equal(standard, 100);
  assert.deepEqual(labels,
    ['gpt-5.6-sol, input (fast)', 'gpt-5.6-sol, priority output']);
});

test('tier overrides are parsed and junk is dropped', () => {
  assert.deepEqual([...overrides(['proj_a=Fast', 'proj_b = standard '])],
                   [['proj_a', 'fast'], ['proj_b', 'standard']]);
  assert.deepEqual([...overrides(['nonsense', '=fast', 'proj_c='])], []);
  assert.deepEqual([...overrides(null)], []);
});
