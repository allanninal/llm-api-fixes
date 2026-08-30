import { test } from 'node:test';
import assert from 'node:assert/strict';
import { countsOf, verdict } from './openai-batch-partial-failure-audit.mjs';

/** A batch object shaped like GET /v1/batches returns them. */
function batch({ status = 'completed', total = 100, completed = 100,
                 failed = 0, ...extra } = {}) {
  return {
    id: 'batch_test',
    status,
    request_counts: { total, completed, failed },
    ...extra,
  };
}

test('completed does not mean every row succeeded', () => {
  const [state, detail] = verdict(batch({ total: 50000, completed: 49131, failed: 869 }));
  assert.equal(state, 'partial');
  assert.match(detail, /869 of 50000/);
  assert.match(detail, /869 line\(s\) shorter/);
});

test('a clean batch needs both halves of the arithmetic', () => {
  assert.equal(verdict(batch({ total: 100, completed: 100, failed: 0 }))[0], 'clean');
  assert.equal(verdict(batch({ total: 100, completed: 99, failed: 1 }))[0], 'partial');
});

test('rows in neither column are their own finding', () => {
  const [state, detail] = verdict(batch({ total: 100, completed: 60, failed: 0 }));
  assert.equal(state, 'unaccounted');
  assert.match(detail, /40 of 100/);
  assert.match(detail, /abandoned/);
});

test('an in flight batch is not reconciled yet', () => {
  for (const status of ['validating', 'in_progress', 'finalizing', 'cancelling']) {
    const [state, detail] = verdict(batch({ status, total: 100, completed: 3 }));
    assert.equal(state, 'running');
    assert.match(detail, /not final/);
  }
});

test('the other terminal states belong to the sibling notes', () => {
  for (const status of ['failed', 'expired', 'cancelled']) {
    assert.equal(verdict(batch({ status, total: 100, completed: 4 }))[0],
                 'other-terminal');
  }
});

test('missing counts are never reported as clean', () => {
  assert.equal(verdict({ id: 'b', status: 'completed' })[0], 'unreadable');
  assert.equal(verdict({ id: 'b', status: 'completed', request_counts: [] })[0],
               'unreadable');
  assert.equal(verdict({ id: 'b' })[0], 'unreadable');
  assert.equal(verdict(batch({ total: 0, completed: 0 }))[0], 'empty');
});

test('counts are read leniently but not invented', () => {
  assert.deepEqual(countsOf({ request_counts: { total: 10 } }), [10, 0, 0]);
  assert.deepEqual(countsOf({ request_counts: { total: '10', completed: '9', failed: '1' } }),
                   [10, 9, 1]);
  assert.equal(countsOf({ request_counts: { total: 'many' } }), null);
  assert.equal(countsOf({}), null);
});
