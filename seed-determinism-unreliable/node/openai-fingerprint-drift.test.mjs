import { test } from 'node:test';
import assert from 'node:assert/strict';
import { byModel, flatten, interleaved, iso, repairLines, transitions, verdict,
         within } from './openai-fingerprint-drift.mjs';

const page = (...rows) => ({ object: 'list', data: rows, has_more: false });
const completion = (id, created, model, system_fingerprint) =>
  ({ id, object: 'chat.completion', created, model, system_fingerprint });

test('two fingerprints in order are the finding with a date', () => {
  const rows = flatten([page(completion('c_1', 1000, 'gpt-5.6-sol', 'fp_aa11'),
                             completion('c_2', 2000, 'gpt-5.6-sol', 'fp_aa11'),
                             completion('c_3', 3000, 'gpt-5.6-sol', 'fp_bb22'))]);
  const entries = byModel(rows)['gpt-5.6-sol'];
  assert.deepEqual(transitions(entries), [[3000, 'fp_aa11', 'fp_bb22']]);
  const [state, detail] = verdict('gpt-5.6-sol', entries);
  assert.equal(state, 'fingerprint-moved');
  assert.ok(detail.includes('2 backend configurations'));
  assert.ok(detail.includes('switching once'));
  assert.equal(iso(3000), '1970-01-01T00:50:00Z');
  assert.ok(repairLines(state).some((l) => l.includes('test oracle')));
});

test('an absent fingerprint is a finding and never a quiet pass', () => {
  const rows = flatten([page(completion('c_1', 1000, 'gpt-5.6-terra', null),
                             completion('c_2', 2000, 'gpt-5.6-terra', ''))]);
  const entries = byModel(rows)['gpt-5.6-terra'];
  assert.deepEqual(transitions(entries), []);
  const [state, detail] = verdict('gpt-5.6-terra', entries);
  assert.equal(state, 'fingerprint-absent');
  assert.ok(detail.includes('even in principle'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('no signal to alarm on')));
  assert.ok(!lines.some((l) => l.includes('stable')));
});

test('an interleaved fleet is separated from one dated switchover', () => {
  const mixed = [{ fingerprint: 'fp_aa11' }, { fingerprint: 'fp_bb22' },
                 { fingerprint: 'fp_aa11' }];
  const once = [{ fingerprint: 'fp_aa11' }, { fingerprint: 'fp_aa11' },
                { fingerprint: 'fp_bb22' }];
  assert.ok(interleaved(mixed));
  assert.ok(!interleaved(once));
  const [state, detail] = verdict('gpt-5.6-sol', mixed);
  assert.equal(state, 'fingerprint-moved');
  assert.ok(detail.includes('more than one configuration is being served at once'));
  assert.ok(repairLines(state, true).some((l) => l.includes('minutes apart')));
  assert.ok(!repairLines(state, false).some((l) => l.includes('minutes apart')));
});

test('one fingerprint is a reading rather than a comparison', () => {
  assert.equal(verdict('gpt-5.6-sol', [{ fingerprint: 'fp_aa11' }])[0],
               'single-observation');
  const steady = Array.from({ length: 40 }, () => ({ fingerprint: 'fp_aa11' }));
  const [state, detail] = verdict('gpt-5.6-sol', steady);
  assert.equal(state, 'fingerprint-stable');
  assert.ok(detail.includes('best effort'));
  assert.ok(repairLines(state).some((l) => l.includes('not a promise')));
});

test('an empty listing points at store and at the responses api', () => {
  const [state, detail] = verdict('(any model)', []);
  assert.equal(state, 'nothing-stored');
  assert.ok(detail.includes('no stored completions'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('store: true')));
  assert.ok(lines.some((l) => l.includes('Responses API') && l.includes('cannot be listed')));
});

test('rows are ordered before transitions are read off them', () => {
  const rows = flatten([page(completion('c_2', 2000, 'm', 'fp_aa11'),
                             completion('c_3', 3000, 'm', 'fp_bb22'),
                             completion('c_1', 1000, 'm', 'fp_aa11'))]);
  assert.equal(transitions(rows).length, 2);
  assert.deepEqual(transitions(byModel(rows).m), [[3000, 'fp_aa11', 'fp_bb22']]);
  assert.equal(within(byModel(rows).m, 1500).length, 2);
  assert.deepEqual(within(rows, 0), rows);
  assert.equal(iso('nonsense'), '');
  assert.equal(iso(null), '');
});
