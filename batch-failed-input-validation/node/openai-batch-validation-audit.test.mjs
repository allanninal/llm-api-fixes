import { test } from 'node:test';
import assert from 'node:assert/strict';
import { batchInputIds, errorRows, failedBatches, linesByCode, mispurposedInputs,
         nothingBilled, repairLines, verdict,
         withinWindow } from './openai-batch-validation-audit.mjs';

const NOW = 1800000000;

const FAILED = {
  id: 'batch_aa',
  status: 'failed',
  created_at: NOW - 3600,
  failed_at: NOW - 3560,
  input_file_id: 'file_in1',
  request_counts: { total: 0, completed: 0, failed: 0 },
  errors: { object: 'list', data: [
    { code: 'invalid_json', message: 'not valid JSON', param: null, line: 41207 },
    { code: 'invalid_json', message: 'not valid JSON', param: null, line: 41208 },
    { code: 'duplicate_custom_id', message: 'custom_id repeated',
      param: 'custom_id', line: 903 },
  ] },
};

const RAN = {
  id: 'batch_bb',
  status: 'completed',
  created_at: NOW - 7200,
  input_file_id: 'file_in2',
  request_counts: { total: 900, completed: 880, failed: 20 },
  output_file_id: 'file_out2',
};

test('a failed batch never ran and names its lines', () => {
  assert.deepEqual(failedBatches([FAILED, RAN]).map((b) => b.id), ['batch_aa']);
  assert.ok(nothingBilled(FAILED));
  assert.ok(!nothingBilled(RAN));
  const groups = linesByCode(errorRows(FAILED));
  assert.deepEqual(groups.invalid_json[0], [41207, 41208]);
  assert.equal(groups.invalid_json[1], 2);
  assert.deepEqual(groups.duplicate_custom_id[0], [903]);
  assert.equal(groups.duplicate_custom_id[3], 'custom_id');
});

test('every field in the errors object is allowed to be missing', () => {
  assert.deepEqual(errorRows({ status: 'failed' }), []);
  assert.deepEqual(errorRows({ errors: null }), []);
  assert.deepEqual(errorRows({ errors: { data: null } }), []);
  assert.deepEqual(errorRows({ errors: { data: ['not an object'] } }), []);
  const rows = errorRows({ errors: { data: [{ code: null, line: null }] } });
  assert.deepEqual(rows, [{ code: 'unknown', message: '', param: null, line: null }]);
  assert.deepEqual(linesByCode(rows).unknown[0], []);
  assert.ok(nothingBilled({ status: 'failed' }));
});

test('a mispurposed input needs all three conditions', () => {
  const files = [
    { id: 'file_x', filename: 'nightly.jsonl', purpose: 'user_data', bytes: 1400000 },
    { id: 'file_ok', filename: 'nightly.jsonl', purpose: 'batch', bytes: 1400000 },
    { id: 'file_in2', filename: 'used.jsonl', purpose: 'user_data', bytes: 10 },
    { id: 'file_img', filename: 'photo.png', purpose: 'vision', bytes: 900 },
    { id: 'file_res', filename: 'out.jsonl', purpose: 'batch_output', bytes: 50 },
  ];
  const used = batchInputIds([FAILED, RAN]);
  assert.deepEqual([...used].sort(), ['file_in1', 'file_in2']);
  const found = mispurposedInputs(files, used);
  assert.deepEqual(found.map((r) => r.id), ['file_x']);
  assert.equal(found[0].purpose, 'user_data');
  assert.deepEqual(mispurposedInputs(files, new Set([...used, 'file_x'])), []);
});

test('rows that failed inside a batch that ran belong to another note', () => {
  assert.deepEqual(failedBatches([RAN]), []);
  const [state, detail] = verdict([], [], 30);
  assert.equal(state, 'validation-clean');
  assert.ok(detail.includes('no batch in the last 30 days'));
});

test('the window is arithmetic on created_at and zero means everything', () => {
  assert.ok(withinWindow(FAILED, NOW, 30));
  assert.ok(!withinWindow(FAILED, NOW + 40 * 86400, 30));
  assert.ok(withinWindow(FAILED, NOW + 40 * 86400, 0));
  assert.ok(!withinWindow({ created_at: 'nonsense' }, NOW, 30));
});

test('the repair names the documented fix for the code it saw', () => {
  const [state, detail] = verdict([FAILED], [{ id: 'file_x' }], 30);
  assert.equal(state, 'validation-failed');
  assert.ok(detail.includes('will not accept'));
  const lines = repairLines(state, ['duplicate_custom_id', 'invalid_json', 'made_up']);
  assert.ok(lines.some((l) => l.includes('custom_id is the only join key')));
  assert.ok(lines.some((l) => l.includes('every line must parse on its own')));
  assert.ok(!lines.some((l) => l.includes('made_up')));
  assert.ok(lines.some((l) => l.includes('receipt, not a result')));
  assert.ok(repairLines('validation-clean', [])[0].startsWith('nothing to change'));
  const [orphanState] = verdict([], [{ id: 'file_x' }], 0);
  assert.equal(orphanState, 'orphan-input-files');
  assert.ok(repairLines(orphanState, []).some((l) => l.includes('purpose matches the endpoint')));
});
