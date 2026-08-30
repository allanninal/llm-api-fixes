import { test } from 'node:test';
import assert from 'node:assert/strict';
import { UNREPORTED, bucketErrors, counts, failureRate, reconcile, repairLines,
         stalled, verdict } from './openai-vector-store-attach-failures.mjs';

const store = ({ total = 0, completed = 0, failed = 0, in_progress = 0,
                 cancelled = 0, status = 'completed' } = {}) =>
  ({ id: 'vs_a1', name: 'handbook', status,
     file_counts: { total, completed, failed, in_progress, cancelled } });

const child = (id, status, code = null, createdAt = 1700000000) =>
  ({ id, object: 'vector_store.file', status, created_at: createdAt,
     vector_store_id: 'vs_a1',
     last_error: code ? { code, message: '...' } : null });

test('a completed store with failed children is the finding', () => {
  const c = counts(store({ total: 849, completed: 812, failed: 37 }));
  const children = [];
  for (let i = 0; i < 19; i += 1) children.push(child(`file-9k${i}`, 'failed', 'unsupported_file'));
  for (let i = 0; i < 14; i += 1) children.push(child(`file-7b${i}`, 'failed', 'invalid_file'));
  for (let i = 0; i < 4; i += 1) children.push(child(`file-2d${i}`, 'failed', 'server_error'));
  const buckets = bucketErrors(children);
  const [state, detail] = verdict(c, buckets, []);
  assert.equal(state, 'attach-failed');
  assert.match(detail, /37 of 849/);
  assert.deepEqual(Object.keys(buckets).sort(),
                   ['invalid_file', 'server_error', 'unsupported_file']);
  const repairs = repairLines(state, buckets);
  assert.ok(repairs.some((l) => l.includes('OCR')));
  assert.ok(repairs.some((l) => l.includes('file_counts.failed == 0')));
});

test('an empty store is handed to the other note by name', () => {
  const c = counts(store({ total: 0 }));
  const [state, detail] = verdict(c, {}, []);
  assert.equal(state, 'no-files');
  assert.match(detail, /empty vector store note/);
  assert.equal(failureRate(c), 0);
  assert.ok(repairLines(state).some((l) => l.includes('vector_store_ids')));
});

test('a failed child with no last_error keeps its own bucket', () => {
  const buckets = bucketErrors([child('file-1', 'failed', 'invalid_file'),
                                child('file-2', 'failed', null),
                                child('file-3', 'completed', null)]);
  assert.deepEqual(buckets[UNREPORTED], ['file-2']);
  assert.deepEqual(buckets.invalid_file, ['file-1']);
  assert.equal(buckets.completed, undefined);
  assert.ok(repairLines('attach-failed', buckets)
    .some((l) => l.includes('has not been looked at')));
});

test('the summary and the listing can disagree', () => {
  const [state, detail] = verdict(
    counts(store({ total: 812, completed: 812, failed: 37 })), {}, []);
  assert.equal(state, 'counts-disagree');
  assert.match(detail, /half-finished repair/);
  assert.ok(repairLines(state).some((l) => l.includes('ingest manifest')));
  assert.deepEqual(reconcile({ failed: 37 }, {}), [37, 0]);
  assert.deepEqual(reconcile({ failed: 2 }, { server_error: ['a', 'b'] }), [2, 2]);
});

test('children pinned in_progress are measured against the clock', () => {
  const now = 1700050000;
  const rows = stalled([child('file-slow', 'in_progress', null, now - 40000),
                        child('file-newer', 'in_progress', null, now - 20000),
                        child('file-fresh', 'in_progress', null, now - 60),
                        child('file-bad', 'in_progress', null, null),
                        child('file-done', 'completed', null, now - 90000)], now);
  assert.deepEqual(rows.map((r) => r[0]), ['file-slow', 'file-newer']);
  const [state, detail] = verdict(
    counts(store({ total: 5, completed: 3, in_progress: 2 })), {}, rows);
  assert.equal(state, 'ingestion-stalled');
  assert.match(detail, /parent stays in_progress/);
  assert.ok(repairLines(state, {}, rows).some((l) => l.includes('file-slow (11h)')));
});

test('a healthy store and a still settling one are not findings', () => {
  assert.equal(verdict(counts(store({ total: 40, completed: 40 })), {}, [])[0],
               'complete');
  assert.equal(verdict(counts(store({ total: 40, completed: 38, in_progress: 2 })),
                       {}, [])[0], 'still-ingesting');
  assert.deepEqual(repairLines('complete'), []);
  assert.deepEqual(bucketErrors(null), {});
  assert.deepEqual(stalled(null, 0), []);
  assert.equal(counts(null).total, 0);
  assert.equal(counts({ file_counts: { total: 'not-a-number' } }).total, 0);
});

test('an unknown error code is reported rather than bucketed away', () => {
  const buckets = bucketErrors([child('file-x', 'failed', 'quota_exceeded')]);
  const lines = repairLines('attach-failed', buckets);
  assert.ok(lines.some((l) => l.includes('quota_exceeded')));
  assert.ok(lines.some((l) => l.includes('three documented values')));
});
