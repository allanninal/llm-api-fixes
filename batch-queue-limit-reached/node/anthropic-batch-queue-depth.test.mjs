import { test } from 'node:test';
import assert from 'node:assert/strict';
import { enqueuedLimit, headroom, queueDepth, queueRows, repairLines, topHolders,
         verdict, workspaceKeys } from './anthropic-batch-queue-depth.mjs';

const RATE_LIMITS = {
  data: [
    { type: 'rate_limit', group_type: 'model_group', models: ['claude-opus-5'],
      limits: [{ type: 'requests_per_minute', value: 4000 },
               { type: 'input_tokens_per_minute', value: 10000000 }] },
    { type: 'rate_limit', group_type: 'batch', models: null,
      limits: [{ type: 'enqueued_batch_requests', value: 300000 }] },
  ],
  next_page: null,
};

const BATCHES = [
  { id: 'msgbatch_01Rf', processing_status: 'in_progress',
    request_counts: { processing: 214900, succeeded: 0, errored: 0, canceled: 0,
                      expired: 0 } },
  { id: 'msgbatch_01Qa', processing_status: 'in_progress',
    request_counts: { processing: 58400, succeeded: 0, errored: 0, canceled: 0,
                      expired: 0 } },
  { id: 'msgbatch_01Zc', processing_status: 'canceling',
    request_counts: { processing: 9600, succeeded: 200, errored: 0, canceled: 0,
                      expired: 0 } },
  { id: 'msgbatch_01Done', processing_status: 'ended',
    request_counts: { processing: 0, succeeded: 50000, errored: 0, canceled: 0,
                      expired: 0 } },
];

test('the ceiling comes out of the batch group and nowhere else', () => {
  assert.equal(enqueuedLimit(RATE_LIMITS), 300000);
  assert.equal(enqueuedLimit({ data: [RATE_LIMITS.data[0]] }), null);
  assert.equal(enqueuedLimit({}), null);
  assert.equal(enqueuedLimit({ data: [{ group_type: 'batch',
    limits: [{ type: 'other', value: 1 }] }] }), null);
  assert.equal(enqueuedLimit({ data: [{ group_type: 'batch',
    limits: [{ type: 'enqueued_batch_requests', value: 'lots' }] }] }), null);
});

test('only live batches and only the processing count are the queue', () => {
  const rows = queueRows(BATCHES, 'ws1');
  assert.deepEqual(rows.map((r) => r.id),
    ['msgbatch_01Rf', 'msgbatch_01Qa', 'msgbatch_01Zc']);
  assert.ok(rows.every((r) => r.id !== 'msgbatch_01Done'));
  assert.equal(rows[2].status, 'canceling');
  assert.equal(queueDepth(rows), 282900);
  assert.equal(queueDepth([]), 0);
});

test('occupancy is measured against the threshold that was passed in', () => {
  const rows = queueRows(BATCHES);
  const depth = queueDepth(rows);
  const [remaining, occupancy] = headroom(depth, 300000);
  assert.equal(remaining, 17100);
  assert.equal(Number(occupancy.toFixed(3)), 0.943);
  const [state, detail] = verdict(depth, 300000, rows, 1, 80);
  assert.equal(state, 'queue-near-limit');
  assert.ok(detail.includes('94% of the ceiling'));
  assert.equal(verdict(depth, 300000, rows, 1, 95)[0], 'queue-clear');
  const [state2, detail2] = verdict(300000, 300000, rows, 1, 80);
  assert.equal(state2, 'queue-exhausted');
  assert.ok(detail2.includes('being refused'));
  assert.deepEqual(headroom(10, null), [null, null]);
  assert.deepEqual(headroom(10, 0), [null, null]);
});

test('an unreadable ceiling is a finding with its own repair', () => {
  const rows = queueRows(BATCHES);
  const [state, detail] = verdict(queueDepth(rows), null, rows, 1, 80);
  assert.equal(state, 'queue-limit-unknown');
  assert.ok(detail.includes('could not be read') && detail.includes('282900'));
  const lines = repairLines(state, rows, null);
  assert.ok(lines.some((l) => l.includes('Workspace keys are rejected by every Admin endpoint')));
  assert.ok(lines.some((l) => l.includes('raw count')));
});

test('the same workspace key twice does not double the depth', () => {
  assert.deepEqual(workspaceKeys('k1', 'k2,k3'), ['k1', 'k2', 'k3']);
  assert.deepEqual(workspaceKeys('k1', 'k1, k1 ,'), ['k1']);
  assert.deepEqual(workspaceKeys('', null), []);
  assert.deepEqual(workspaceKeys(null, 'k9'), ['k9']);
});

test('the repair names the biggest holder and the per batch cap', () => {
  const rows = queueRows(BATCHES);
  const lines = repairLines('queue-near-limit', rows, 300000);
  assert.ok(lines.some((l) => l.includes('msgbatch_01Rf alone holds 214900 of the 300000')));
  assert.ok(lines.some((l) => l.includes('100000 requests or 256 MB')));
  assert.ok(lines.some((l) => l.includes('24 hour window')));
  assert.equal(topHolders(rows, 1)[0].id, 'msgbatch_01Rf');
  assert.deepEqual(topHolders([], 3), []);
  assert.ok(repairLines('queue-clear', rows, 300000)[0].startsWith('nothing to change'));
});
