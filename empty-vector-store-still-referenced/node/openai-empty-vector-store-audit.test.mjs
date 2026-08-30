import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cause, classify, configuredIds, counts, emptiness, repairLines,
         usageBytes } from './openai-empty-vector-store-audit.mjs';

const store = ({ total = 0, completed = 0, failed = 0, in_progress = 0,
                 bytes = 0, status = 'completed', id = 'vs_a1',
                 name = 'handbook' } = {}) =>
  ({ id, name, status, usage_bytes: bytes,
     file_counts: { total, completed, failed, in_progress, cancelled: 0 } });

test('a configured store with nothing in it is the finding', () => {
  const empty = store({ total: 0 });
  assert.equal(emptiness(empty), 'no-files');
  const [state, detail] = classify(empty, true);
  assert.equal(state, 'referenced-empty');
  assert.match(detail, /0 file\(s\) attached/);
  assert.equal(cause(empty), 'never-ingested');
  assert.ok(repairLines(state, cause(empty)).some((l) => l.includes('refuse to boot')));
});

test('attached but never indexed is the other note', () => {
  const broken = store({ total: 40, completed: 0, failed: 40 });
  assert.equal(emptiness(broken), 'nothing-completed');
  const [state, detail] = classify(broken, true);
  assert.equal(state, 'referenced-nothing-indexed');
  assert.match(detail, /40 attached, 0 completed/);
  assert.equal(cause(broken), 'attach-failed');
  assert.ok(repairLines(state, cause(broken)).some((l) => l.includes('last_error.code')));
});

test('an expired store is empty for a reason that will recur', () => {
  const gone = store({ total: 0, status: 'expired' });
  assert.equal(cause(gone), 'expired');
  assert.ok(repairLines('referenced-empty', cause(gone))
    .some((l) => l.includes('same schedule')));
  assert.deepEqual(counts(gone), counts(store({ total: 0 })));
});

test('an empty store nobody references is not a finding', () => {
  const [state, detail] = classify(store({ total: 0 }), false);
  assert.equal(state, 'abandoned-empty');
  assert.match(detail, /litter/);
  assert.equal(classify(store({ total: 9, completed: 9, bytes: 1024 }), false)[0],
               'unreferenced');
});

test('an id that does not resolve blames the project first', () => {
  const [state, detail] = classify(null, true);
  assert.equal(state, 'referenced-missing');
  assert.match(detail, /project scoped/);
  assert.match(repairLines(state)[0], /project/);
  assert.equal(classify(undefined, false)[0], 'not-found');
});

test('completed files with no bytes is named rather than guessed', () => {
  const odd = store({ total: 9, completed: 9, bytes: 0 });
  assert.equal(emptiness(odd), 'zero-bytes');
  const [state, detail] = classify(odd, true);
  assert.equal(state, 'referenced-zero-bytes');
  assert.match(detail, /disagree/);
  assert.ok(repairLines(state).some((l) => l.includes('before deciding')));
});

test('configuredIds survives the trailing comma', () => {
  assert.deepEqual(configuredIds('vs_a1,vs_b2,'), ['vs_a1', 'vs_b2']);
  assert.deepEqual(configuredIds('vs_a1 vs_b2\nvs_a1'), ['vs_a1', 'vs_b2']);
  assert.deepEqual(configuredIds(null, ['vs_c3'], 'vs_c3'), ['vs_c3']);
  assert.deepEqual(configuredIds(''), []);
  assert.deepEqual(configuredIds(), []);
});

test('a grounded store reports its size', () => {
  const good = store({ total: 812, completed: 812, bytes: 43200512 });
  const [state, detail] = classify(good, true);
  assert.equal(state, 'grounded');
  assert.match(detail, /41\.2 MiB/);
  assert.deepEqual(repairLines(state), []);
  assert.equal(usageBytes({ usage_bytes: 'nope' }), 0);
  assert.equal(emptiness(null), 'no-files');
});
