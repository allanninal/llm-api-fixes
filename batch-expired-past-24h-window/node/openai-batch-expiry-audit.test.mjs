import { test } from 'node:test';
import assert from 'node:assert/strict';
import { countsOf, deadline, verdict } from './openai-batch-expiry-audit.mjs';

// 2026-08-30T00:00:00Z. Fixed, because every state here is a subtraction from it.
const NOW = 1788048000;
const HOUR = 3600;

function batch({ status = 'in_progress', total = 20000, completed = 8000,
                 ...extra } = {}) {
  return {
    id: 'batch_test',
    status,
    request_counts: { total, completed, failed: 0 },
    ...extra,
  };
}

test('an expired batch reports the rows that never ran', () => {
  const [state, detail] = verdict(
    batch({ status: 'expired', total: 50000, completed: 20000,
            expired_at: NOW - HOUR }), NOW);
  assert.equal(state, 'expired');
  assert.match(detail, /30000 row\(s\) unfinished/);
  assert.match(detail, /batch_expired/);
});

test('a batch close to its deadline is the useful finding', () => {
  const [state, detail] = verdict(batch({ expires_at: NOW + 2 * HOUR }), NOW, 4);
  assert.equal(state, 'expiring-soon');
  assert.match(detail, /2\.0 hour\(s\) of window left/);
  assert.match(detail, /second batch/);
});

test('a batch with room left is left alone', () => {
  const [state, detail] = verdict(batch({ expires_at: NOW + 23 * HOUR }), NOW, 4);
  assert.equal(state, 'in-flight');
  assert.match(detail, /23\.0 hour\(s\)/);
});

test('a window that closed while the status still says running', () => {
  const [state, detail] = verdict(batch({ expires_at: NOW - HOUR }), NOW);
  assert.equal(state, 'overdue');
  assert.match(detail, /1\.0 hour\(s\) past/);
});

test('the deadline says which timestamp it came from', () => {
  assert.deepEqual(deadline({ expires_at: NOW }), [NOW, 'expires_at']);
  const [started, startedSource] = deadline({ in_progress_at: NOW - HOUR });
  assert.equal(started, NOW - HOUR + 86400);
  assert.equal(startedSource, 'in_progress_at plus 24h');
  const [created, createdSource] = deadline({ created_at: NOW - HOUR });
  assert.equal(created, NOW - HOUR + 86400);
  assert.match(createdSource, /upper bound/);
  assert.equal(deadline({ id: 'b' })[0], null);
});

test('expires_at wins over the fallbacks', () => {
  const [when, source] = deadline({
    created_at: NOW - 6 * HOUR,
    in_progress_at: NOW - HOUR,
    expires_at: NOW + 2 * HOUR,
  });
  assert.equal(when, NOW + 2 * HOUR);
  assert.equal(source, 'expires_at');
});

test('settled and unreadable batches are not findings', () => {
  for (const status of ['completed', 'failed', 'cancelled']) {
    assert.equal(verdict(batch({ status }), NOW)[0], 'settled');
  }
  assert.equal(verdict(batch({ status: 'teleporting' }), NOW)[0], 'unreadable');
  assert.equal(verdict(batch(), NOW)[0], 'unreadable');
  assert.deepEqual(countsOf({ request_counts: { total: 5, completed: 5 } }), [5, 5]);
  assert.equal(countsOf({}), null);
});
