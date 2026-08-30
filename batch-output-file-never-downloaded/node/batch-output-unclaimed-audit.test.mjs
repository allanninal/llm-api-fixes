import { test } from 'node:test';
import assert from 'node:assert/strict';
import { anthropicRows, byUrgency, countsByState, daysLeft, fileIndex,
         openaiDeadline, openaiRows, parseTime, readLedger, repairLines,
         verdict } from './batch-output-unclaimed-audit.mjs';

const NOW = 1800000000;
const DAY = 86400;

const OPENAI_BATCHES = [
  { id: 'batch_fresh', status: 'completed', created_at: NOW - 26 * DAY,
    completed_at: NOW - 26 * DAY, output_file_id: 'file_soon',
    request_counts: { total: 88300, completed: 88300, failed: 0 } },
  { id: 'batch_gone', status: 'completed', created_at: NOW - 60 * DAY,
    completed_at: NOW - 60 * DAY, output_file_id: 'file_2b7c',
    request_counts: { total: 40000, completed: 40000, failed: 0 } },
  { id: 'batch_open', status: 'completed', created_at: NOW - 3 * DAY,
    completed_at: NOW - 3 * DAY, output_file_id: 'file_room',
    request_counts: { total: 90000, completed: 90000, failed: 0 } },
  { id: 'batch_stuck', status: 'in_progress', created_at: NOW - 62 * 3600 },
];

const OPENAI_FILES = [
  { id: 'file_soon', purpose: 'batch_output', bytes: 10, created_at: NOW - 26 * DAY },
  { id: 'file_room', purpose: 'batch_output', bytes: 10, created_at: NOW - 3 * DAY },
];

const ANTHROPIC_BATCHES = [
  { id: 'msgbatch_arch', processing_status: 'ended',
    created_at: '2026-01-02T00:00:00Z', ended_at: '2026-01-02T04:00:00Z',
    archived_at: '2026-01-31T00:00:00Z', results_url: null,
    request_counts: { processing: 0, succeeded: 12400, errored: 0, canceled: 0,
                      expired: 0 } },
  { id: 'msgbatch_open', processing_status: 'in_progress',
    created_at: '2026-01-02T00:00:00Z',
    request_counts: { processing: 500, succeeded: 0, errored: 0, canceled: 0,
                      expired: 0 } },
];

test('the retention anchors are different on each provider', () => {
  const index = fileIndex(OPENAI_FILES);
  const [deadline, source] = openaiDeadline(OPENAI_BATCHES[0], index.file_soon);
  assert.equal(source, 'completed_at + 30d');
  assert.equal(daysLeft(deadline, NOW), 4);
  const stamped = { ...index.file_soon, expires_at: NOW + 2 * DAY };
  const [d2, s2] = openaiDeadline(OPENAI_BATCHES[0], stamped);
  assert.equal(s2, 'expires_at');
  assert.equal(daysLeft(d2, NOW), 2);
  assert.deepEqual(openaiDeadline({}, {}), [null, 'unknown']);
  assert.equal(daysLeft(null, NOW), null);
  const created = parseTime('2026-01-02T00:00:00Z');
  const rows = anthropicRows([{ ...ANTHROPIC_BATCHES[0], archived_at: null }],
    new Set(), created + 27 * DAY, 5);
  assert.equal(rows[0].state, 'expiring');
  assert.ok(rows[0].detail.includes('created_at + 29d'));
});

test('a missing output file is lost and not merely unclaimed', () => {
  const rows = openaiRows(OPENAI_BATCHES, fileIndex(OPENAI_FILES), new Set(), NOW, 5);
  const states = Object.fromEntries(rows.map((r) => [r.id, r.state]));
  assert.equal(states.batch_gone, 'lost');
  assert.equal(states.batch_fresh, 'expiring');
  assert.equal(states.batch_open, 'unclaimed');
  assert.ok(rows.find((r) => r.state === 'lost').detail.includes('no longer exists'));
  const arch = anthropicRows(ANTHROPIC_BATCHES, new Set(), NOW, 5);
  assert.deepEqual(arch.filter((r) => r.id === 'msgbatch_arch').map((r) => r.state),
    ['lost']);
});

test('never polled never fetched and never claimed are one pass', () => {
  const rows = [...openaiRows(OPENAI_BATCHES, fileIndex(OPENAI_FILES), new Set(), NOW, 5),
    ...anthropicRows(ANTHROPIC_BATCHES, new Set(), NOW, 5)];
  const counts = countsByState(rows);
  assert.equal(counts.stalled, 2);
  assert.ok(rows.filter((r) => r.state === 'stalled')
    .some((r) => r.detail.includes('past the 24 h window')));
  assert.equal(counts.unclaimed, 1);
  const claimed = openaiRows([OPENAI_BATCHES[2]], fileIndex(OPENAI_FILES),
    new Set(['batch_open']), NOW, 5);
  assert.equal(claimed[0].state, 'claimed');
  assert.ok(claimed[0].detail.includes('in the ingest ledger'));
});

test('the verdict leads with what you can still act on', () => {
  const rows = [...openaiRows(OPENAI_BATCHES, fileIndex(OPENAI_FILES), new Set(), NOW, 5),
    ...anthropicRows(ANTHROPIC_BATCHES, new Set(), NOW, 5)];
  const [state, detail] = verdict(rows, new Set(['x']), 5);
  assert.equal(state, 'batch-output-expiring');
  assert.ok(detail.includes('expire within 5 days'));
  assert.ok(detail.includes('already unrecoverable') && detail.includes('never claimed'));
  const ordered = byUrgency(rows).map((r) => r.state);
  assert.equal(ordered[0], 'expiring');
  assert.ok(ordered.indexOf('lost') < ordered.indexOf('unclaimed'));
  assert.ok(ordered.indexOf('unclaimed') < ordered.indexOf('stalled'));
  const noExpiring = rows.filter((r) => r.state !== 'expiring');
  assert.equal(verdict(noExpiring, new Set(['x']), 5)[0], 'batch-output-lost');
  assert.equal(verdict([], new Set(), 5)[0], 'batch-output-clean');
});

test('an absent ledger is reported rather than assumed away', () => {
  const rows = openaiRows([OPENAI_BATCHES[2]], fileIndex(OPENAI_FILES), new Set(), NOW, 5);
  const [state, detail] = verdict(rows, new Set(), 5);
  assert.equal(state, 'batch-output-unclaimed');
  assert.ok(detail.includes('no ingest ledger was supplied'));
  assert.ok(repairLines(state, rows, new Set())
    .some((l) => l.includes('neither API offers a read receipt')));
  assert.deepEqual([...readLedger('# note\nbatch_a\nbatch_b,batch_a\n')].sort(),
    ['batch_a', 'batch_b']);
  assert.equal(readLedger('').size, 0);
});

test('the repair hands the other half to the error file note', () => {
  const rows = [...openaiRows(OPENAI_BATCHES, fileIndex(OPENAI_FILES), new Set(), NOW, 5),
    ...anthropicRows(ANTHROPIC_BATCHES, new Set(), NOW, 5)];
  const lines = repairLines('batch-output-expiring', rows, new Set(['x']));
  assert.ok(lines.some((l) => l.includes('error_file_id, the list of rows that failed')));
  assert.ok(lines.some((l) => l.includes('download the expiring outputs today')));
  assert.ok(lines.some((l) => l.includes('re-run and re-paid')));
  assert.ok(lines.some((l) => l.includes('stale object rather than')));
  assert.ok(repairLines('batch-output-clean', [], new Set(['x']))[0]
    .startsWith('nothing outstanding'));
});
