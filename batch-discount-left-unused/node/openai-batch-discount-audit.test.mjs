import { test } from 'node:test';
import assert from 'node:assert/strict';
import { accumulate, concentration, saving, syncCost, verdict }
  from './openai-batch-discount-audit.mjs';

function bucket(...results) {
  return { start_time: 0, end_time: 3600, results };
}

function result({ project = 'proj_a', model = 'gpt-5.6-terra', batch = false,
                  made = 0, inputTokens = 0, outputTokens = 0 } = {}) {
  return {
    project_id: project,
    model,
    batch,
    num_model_requests: made,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
  };
}

test('idle hours stay in the denominator', () => {
  const buckets = [bucket(result({ made: 0 })), bucket(result({ made: 4000 })),
                   bucket(), bucket(result({ made: 0 }))];
  const row = accumulate(buckets).get('proj_a / gpt-5.6-terra');
  assert.deepEqual(row.hourly, [0, 4000, 0, 0]);
  assert.equal(row.sync_requests, 4000);
});

test('batch and synchronous traffic are kept apart', () => {
  const rows = accumulate([bucket(
    result({ made: 100, inputTokens: 50, batch: false }),
    result({ made: 900, batch: true }))]);
  const row = rows.get('proj_a / gpt-5.6-terra');
  assert.equal(row.sync_requests, 100);
  assert.equal(row.batch_requests, 900);
  assert.equal(row.sync_input, 50);
  assert.deepEqual(row.hourly, [100]);
});

test('concentration separates a schedule from an audience', () => {
  const spiky = [...new Array(18).fill(0), 4000, 1000];
  assert.equal(concentration(spiky, 0.10), 1.0);
  assert.equal(concentration(new Array(20).fill(250), 0.10), 0.1);
  assert.equal(concentration([], 0.10), null);
  assert.equal(concentration([0, 0, 0], 0.10), null);
});

test('a nightly job on the synchronous endpoint is the finding', () => {
  const row = { sync_requests: 5000, batch_requests: 0,
                hourly: [...new Array(18).fill(0), 4000, 1000] };
  const [state, detail] = verdict(row);
  assert.equal(state, 'batch-shaped');
  assert.match(detail, /100% of 5000 synchronous request\(s\)/);
  assert.match(detail, /paying interactive prices/);
});

test('spread out traffic is correctly synchronous', () => {
  const row = { sync_requests: 5000, batch_requests: 0,
                hourly: new Array(20).fill(250) };
  const [state, detail] = verdict(row);
  assert.equal(state, 'interactive');
  assert.match(detail, /right one/);
});

test('the three answers that are not findings', () => {
  assert.equal(verdict({ sync_requests: 10, batch_requests: 0, hourly: [10] })[0],
               'too-little-traffic');
  assert.equal(verdict({ sync_requests: 100, batch_requests: 9900, hourly: [100] })[0],
               'already-batched');
  assert.equal(verdict({ sync_requests: 5000, batch_requests: 0, hourly: [] })[0],
               'unmeasurable');
});

test('the money comes from the cost report not a price table', () => {
  const costs = [{ results: [
    { project_id: 'proj_a', line_item: 'gpt-5.6-terra, input',
      amount: { value: 300.0, currency: 'usd' } },
    { project_id: 'proj_a', line_item: 'gpt-5.6-terra, batch input',
      amount: { value: 40.0, currency: 'usd' } },
    { project_id: 'proj_b', line_item: 'gpt-5.6-terra, input',
      amount: { value: 99.0, currency: 'usd' } },
  ] }];
  assert.equal(syncCost(costs, 'proj_a'), 300.0);
  assert.equal(syncCost(costs), 399.0);
  assert.equal(saving(300.0), 150.0);
  assert.equal(saving(0), 0);
  assert.equal(saving(null), null);
});
