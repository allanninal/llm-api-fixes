import { test } from 'node:test';
import assert from 'node:assert/strict';
import { INFERRED, MEASURED, byModel, contrast, flatten, keyState, repairLines,
         verdict } from './openai-streaming-verification-probe.mjs';

const bucket = (...results) => ({ start_time: 1700000000, results });
const result = (model, keyId, requests, inputTokens = 0, outputTokens = 0) =>
  ({ model, api_key_id: keyId, num_model_requests: requests,
     input_tokens: inputTokens, output_tokens: outputTokens });

test('two keys disagreeing on one model is the finding', () => {
  const rows = flatten([bucket(result('gpt-5.6', 'key_9fA2', 1204),
                               result('gpt-5.6', 'key_3bQ7', 900, 400000, 812004))]);
  const perKey = byModel(rows)['gpt-5.6'];
  const [state, detail] = verdict(200, perKey);
  assert.equal(state, 'verification-suspected');
  assert.ok(detail.includes('key_9fA2') && detail.includes('key_3bQ7'));
  assert.ok(detail.includes('1,204') && detail.includes('812,004'));
});

test('every key mute is the other note and says so', () => {
  const perKey = byModel(flatten([bucket(result('o4-mini', 'key_a', 400),
                                         result('o4-mini', 'key_b', 900),
                                         result('o4-mini', 'key_c', 30))]))['o4-mini'];
  const [state, detail] = verdict(200, perKey);
  assert.equal(state, 'model-wide-mute');
  assert.ok(detail.includes('every caller sends'));
  assert.ok(repairLines(state).some((l) => l.includes('reasoning-model parameter note')));
});

test('one key on a model is unresolvable and is not graded', () => {
  const perKey = byModel(flatten([bucket(result('gpt-5.1', 'key_only', 800))]))['gpt-5.1'];
  const [state, detail] = verdict(200, perKey);
  assert.equal(state, 'single-key-model');
  assert.ok(detail.includes('nothing to compare it against'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('canary')));
  assert.ok(lines.some((l) => l.startsWith('measured:')));
});

test('a model that does not resolve belongs to the model list note', () => {
  const perKey = byModel(flatten([bucket(result('gpt-4-0613', 'key_a', 500),
                                         result('gpt-4-0613', 'key_b', 500, 1, 9))]))['gpt-4-0613'];
  const [state, detail] = verdict(404, perKey);
  assert.equal(state, 'model-not-visible');
  assert.ok(detail.includes('model-list note'));
  assert.ok(repairLines(state).some((l) => l.includes('GET /v1/models')));
});

test('rejected before generation is not the same as produced nothing', () => {
  assert.equal(keyState({ requests: 100, input: 0, output: 0 }), 'mute');
  assert.equal(keyState({ requests: 100, input: 900, output: 0 }), 'no-output');
  assert.equal(keyState({ requests: 100, input: 900, output: 4 }), 'producing');
  assert.equal(keyState({ requests: 0, input: 0, output: 0 }), 'idle');
  assert.equal(keyState({ requests: 5, input: 0, output: 0 }, 20), 'idle');

  const perKey = byModel(flatten([bucket(result('m', 'key_a', 100, 900, 0))])).m;
  assert.equal(contrast(perKey)[0], 'input-without-output');
  assert.ok(repairLines('input-without-output').some((l) => l.includes('truncation or a refusal')));
});

test('the finding separates what was measured from what was inferred', () => {
  const lines = repairLines('verification-suspected');
  assert.equal(lines[0], 'measured: ' + MEASURED);
  assert.equal(lines[1], 'inferred: ' + INFERRED);
  assert.ok(INFERRED.includes('No endpoint reports verification state'));
  assert.ok(lines.some((l) => l.includes('15 minutes')));
  assert.ok(lines.some((l) => l.includes('unset stream')));
  assert.ok(lines.some((l) => l.includes('already verified')));
});

test('counts are coerced and missing fields do not become silence', () => {
  const rows = flatten([bucket({ model: null, api_key_id: null,
                                 num_model_requests: 'not-a-number' })]);
  assert.deepEqual(rows, [['(unattributed)', '(unattributed)', 0, 0, 0]]);
  assert.deepEqual(flatten(null), []);
  assert.deepEqual(byModel(null), {});
  assert.equal(contrast({})[0], 'no-traffic');
  assert.ok(verdict(null, {})[1].endsWith('to rule out access)'));
  assert.deepEqual(repairLines('healthy'), []);
});
