import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiTotals, compare, recordedTokens, untrackedCost }
  from './openai-streaming-usage-gap.mjs';

function bucket(...results) {
  return { start_time: 0, end_time: 86400, results };
}

function usage({ project = 'proj_chat', inputTokens = 0, outputTokens = 0,
                 requests = 0 } = {}) {
  return { project_id: project, input_tokens: inputTokens,
           output_tokens: outputTokens, num_model_requests: requests };
}

test('a dashboard short of the org report is the finding', () => {
  const [state, detail] = compare(42000000, 28000000);
  assert.equal(state, 'undercount');
  assert.match(detail, /short by 14000000/);
  assert.match(detail, /33\.3%/);
  assert.match(detail, /usage: null/);
});

test('recording more than you were billed for is a different bug', () => {
  const [state, detail] = compare(10000000, 13000000);
  assert.equal(state, 'overcount');
  assert.match(detail, /double counting/);
});

test('a project missing from telemetry is not an undercount', () => {
  const [state, detail] = compare(9000000, null);
  assert.equal(state, 'untracked');
  assert.match(detail, /nothing here is being recorded/);
  assert.equal(compare(9000000, 0)[0], 'undercount');
});

test('tokens recorded against a project with no usage are a mapping bug', () => {
  const [state, detail] = compare(0, 5000000);
  assert.equal(state, 'phantom');
  assert.match(detail, /project id mapping/);
  assert.equal(compare(0, null)[0], 'idle');
  assert.equal(compare(0, 0)[0], 'idle');
});

test('small projects and close numbers are not findings', () => {
  assert.equal(compare(5000, 1)[0], 'too-little-traffic');
  const [state, detail] = compare(1000000, 980000);
  assert.equal(state, 'matched');
  assert.match(detail, /2\.0% apart/);
});

test('usage buckets fold into one row per project', () => {
  const rows = apiTotals([
    bucket(usage({ inputTokens: 100, outputTokens: 20, requests: 3 }),
           usage({ project: 'proj_batch', inputTokens: 7, outputTokens: 1 })),
    bucket(usage({ inputTokens: 50, outputTokens: 5, requests: 2 })),
  ]);
  assert.deepEqual(rows.get('proj_chat'), { tokens: 175, requests: 5 });
  assert.deepEqual(rows.get('proj_batch'), { tokens: 8, requests: 0 });
});

test('telemetry is read leniently but absence is preserved', () => {
  assert.equal(recordedTokens(1200), 1200);
  assert.equal(recordedTokens({ tokens: 1200 }), 1200);
  assert.equal(recordedTokens({ input_tokens: 900, output_tokens: 300 }), 1200);
  assert.equal(recordedTokens(0), 0);
  assert.equal(recordedTokens(null), null);
  assert.equal(recordedTokens({}), null);
  assert.equal(recordedTokens('lots'), null);
  assert.equal(recordedTokens(true), null);
});

test('the money is a pro rata share of reported spend', () => {
  const costs = [bucket(
    { project_id: 'proj_chat', amount: { value: 300.0, currency: 'usd' } },
    { project_id: 'proj_other', amount: { value: 900.0, currency: 'usd' } },
  )];
  assert.equal(untrackedCost(costs, 'proj_chat', 1000000, 250000), 75);
  assert.equal(untrackedCost(costs, 'proj_chat', 1000000, 0), 0);
  assert.equal(untrackedCost(costs, 'proj_chat', 0, 100), 0);
  assert.equal(untrackedCost(costs, 'proj_missing', 1000000, 500000), 0);
});
