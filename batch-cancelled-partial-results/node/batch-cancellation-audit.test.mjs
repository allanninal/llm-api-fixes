import { test } from 'node:test';
import assert from 'node:assert/strict';
import { anthropicCancelRows, openaiCancelRows, parseTime, repairLines,
         salvageRows, salvagedTotal, stuckRows,
         verdict } from './batch-cancellation-audit.mjs';

const NOW = 1800000000;

const OPENAI = [
  { id: 'batch_c1', status: 'cancelled',
    request_counts: { total: 90000, completed: 61204, failed: 0 },
    output_file_id: 'file_7ac1', cancelling_at: NOW - 7200, cancelled_at: NOW - 6900 },
  { id: 'batch_c2', status: 'cancelling',
    request_counts: { total: 400, completed: 0, failed: 0 },
    cancelling_at: NOW - 68 * 60 },
  { id: 'batch_ok', status: 'completed',
    request_counts: { total: 10, completed: 10, failed: 0 } },
];

const ANTHROPIC = [
  { id: 'msgbatch_01Hq', processing_status: 'ended',
    cancel_initiated_at: '2026-08-20T18:37:24.100435Z',
    request_counts: { processing: 0, succeeded: 41880, errored: 0, canceled: 12120,
                      expired: 0 },
    results_url: 'https://api.anthropic.com/v1/messages/batches/x/results' },
  { id: 'msgbatch_02Zz', processing_status: 'in_progress', cancel_initiated_at: null,
    request_counts: { processing: 500, succeeded: 0, errored: 0, canceled: 0,
                      expired: 0 } },
];

test('two providers normalise to one row shape', () => {
  const rows = [...openaiCancelRows(OPENAI), ...anthropicCancelRows(ANTHROPIC)];
  assert.deepEqual(rows.map((r) => r.id), ['batch_c1', 'batch_c2', 'msgbatch_01Hq']);
  assert.equal(rows[0].done, 61204);
  assert.equal(rows[0].stopped, 28796);
  assert.equal(rows[0].total, 90000);
  assert.equal(rows[0].artifact, 'file_7ac1');
  assert.equal(rows[2].done, 41880);
  assert.equal(rows[2].stopped, 12120);
  assert.equal(rows[2].total, 54000);
  assert.equal(salvagedTotal(rows), 61204 + 41880);
  assert.ok(rows.every((r) => r.id !== 'msgbatch_02Zz'));
});

test('the timestamp parser takes both providers and refuses rubbish', () => {
  assert.equal(parseTime(NOW), NOW);
  assert.equal(parseTime('2026-08-20T18:37:24Z'), 1787251044);
  assert.equal(parseTime('2026-08-20T18:37:24.100435Z'), 1787251044);
  assert.equal(parseTime('2026-08-20T18:37:24+00:00'), 1787251044);
  for (const junk of [null, undefined, '', 'yesterday', true, {}]) {
    assert.equal(parseTime(junk), null);
  }
});

test('a stuck cancel is measured against an argument not a clock', () => {
  const rows = openaiCancelRows(OPENAI);
  assert.deepEqual(stuckRows(rows, NOW, 15 * 60).map((r) => r.id), ['batch_c2']);
  assert.deepEqual(stuckRows(rows, NOW, 3 * 3600), []);
  const unknown = [{ id: 'batch_x', inFlight: true, cancelStarted: null, done: 0 }];
  assert.deepEqual(stuckRows(unknown, NOW, 15 * 60), unknown);
  assert.deepEqual(stuckRows([rows[0]], NOW, 1), []);
});

test('an unlanded cancel outranks a salvageable one', () => {
  const rows = [...openaiCancelRows(OPENAI), ...anthropicCancelRows(ANTHROPIC)];
  const stuck = stuckRows(rows, NOW, 15 * 60);
  const salvage = salvageRows(rows);
  const [state, detail] = verdict(rows, stuck, salvage);
  assert.equal(state, 'cancel-stuck');
  assert.ok(detail.includes('mid cancel') && detail.includes('103084 finished rows'));
  const [state2, detail2] = verdict(rows, [], salvage);
  assert.equal(state2, 'cancel-partial-unclaimed');
  assert.ok(detail2.includes('pay for again'));
});

test('a cancel that landed before anything ran is not a finding', () => {
  const early = [{ id: 'batch_z', provider: 'openai', status: 'cancelled',
                   inFlight: false, done: 0, stopped: 400, total: 400,
                   artifact: null, cancelStarted: NOW - 86400 }];
  assert.deepEqual(salvageRows(early), []);
  const [state, detail] = verdict(early, [], []);
  assert.equal(state, 'cancel-clean');
  assert.ok(detail.includes('nothing to salvage'));
  assert.ok(repairLines(state, early)[0].startsWith('nothing to collect'));
  assert.deepEqual(verdict([], [], []),
    ['no-cancels', 'no batch on the providers checked has had a cancellation initiated']);
  assert.deepEqual(repairLines('no-cancels', []), []);
});

test('the repair states the documented billing rule and only that', () => {
  const rows = [...openaiCancelRows(OPENAI), ...anthropicCancelRows(ANTHROPIC)];
  const lines = repairLines('cancel-partial-unclaimed', rows);
  assert.ok(lines.some((l) => l.includes('custom_id is the only join key')));
  assert.ok(lines.some((l) => l.includes('canceled and expired requests are not billed')));
  assert.ok(lines.some((l) => l.includes('not documented') && l.includes('floor')));
  const lines2 = repairLines('cancel-partial-unclaimed', anthropicCancelRows(ANTHROPIC));
  assert.ok(!lines2.some((l) => l.includes('floor')));
  assert.ok(repairLines('cancel-stuck', rows)
    .some((l) => l.includes('cancelling or canceling has not stopped')));
});
