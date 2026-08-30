import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  classify, groupKey, refusalRate, refusals, repairLines, stopReason, visibleText,
} from './openai-refusal-channel.mjs';

const refused = ({ text = "I'm sorry, I can't help with that.",
  preamble = null, metadata = {} } = {}) => {
  const content = [];
  if (preamble) content.push({ type: 'output_text', text: preamble });
  content.push({ type: 'refusal', refusal: text });
  return { id: 'resp_r', status: 'completed', model: 'gpt-5.1', metadata,
    output: [{ type: 'message', content }] };
};

const answered = ({ text = '{"ok": true}', metadata = {} } = {}) => ({
  id: 'resp_a', status: 'completed', model: 'gpt-5.1', metadata,
  output: [{ type: 'message', content: [{ type: 'output_text', text }] }],
});

test('a refusal is a completed answer with nothing to parse', () => {
  const response = refused();
  assert.equal(stopReason(response), null);
  assert.equal(visibleText(response), '');
  assert.deepEqual(refusals(response),
    [{ index: 0, text: "I'm sorry, I can't help with that." }]);

  const [state, detail] = classify(response);
  assert.equal(state, 'refused');
  assert.match(detail, /nothing went wrong/);
  assert.match(repairLines(state)[0], /first-class branch before parsing/);
});

test('a refusal that follows a preamble is not an answer either', () => {
  const response = refused({ preamble: 'Here is what I found so far. ' });
  const [state, detail] = classify(response);
  assert.equal(state, 'refused-after-partial');
  assert.match(detail, /storing the preamble/);
  assert.equal(visibleText(response), 'Here is what I found so far.');
});

test('the chat completions shape is read as well', () => {
  const legacy = { choices: [{ finish_reason: 'stop',
    message: { content: null, refusal: "I can't assist with that." } }] };
  assert.equal(refusals(legacy)[0].text, "I can't assist with that.");
  assert.equal(visibleText(legacy), '');
  assert.equal(classify(legacy)[0], 'refused');
});

test('a filter stop is counted apart from a model refusal', () => {
  const filtered = { status: 'incomplete',
    incomplete_details: { reason: 'content_filter' }, output: [] };
  const [state, detail] = classify(filtered);
  assert.equal(state, 'stopped-by-filter');
  assert.match(detail, /not the model declining it/);
  assert.match(repairLines(state)[1], /separately from model refusals/);
});

test('a truncated response is handed to the other note', () => {
  const cut = { status: 'incomplete',
    incomplete_details: { reason: 'max_output_tokens' },
    output: [{ type: 'message', content: [{ type: 'output_text', text: '{"a": 1' }] }] };
  const [state, detail] = classify(cut);
  assert.equal(state, 'truncated');
  assert.match(detail, /Nothing was refused/);
  assert.match(repairLines(state)[0], /interrupted, not unwilling/);
});

test('the rate is grouped by template and withheld below the floor', () => {
  const rows = [];
  for (let i = 0; i < 9; i += 1) rows.push(['kyc-extract', 'refused']);
  for (let i = 0; i < 21; i += 1) rows.push(['kyc-extract', 'answered']);
  rows.push(['rare-path', 'refused']);

  const rates = refusalRate(rows);
  assert.deepEqual([...rates.keys()].sort(), ['kyc-extract', 'rare-path']);
  assert.equal(rates.get('kyc-extract').total, 30);
  assert.equal(rates.get('kyc-extract').refused, 9);
  assert.ok(Math.abs(rates.get('kyc-extract').rate - 0.3) < 1e-9);
  assert.equal(rates.get('rare-path').total, 1);
  assert.equal(rates.get('rare-path').rate, null);
});

test('grouping falls back without pretending it is sharp', () => {
  assert.equal(groupKey(refused({ metadata: { template: 'kyc-extract' } })), 'kyc-extract');
  assert.equal(groupKey({ prompt: { id: 'pmpt_9' } }), 'prompt:pmpt_9');
  assert.equal(groupKey({ model: 'gpt-5.1' }), 'model:gpt-5.1');
  assert.equal(groupKey({}), 'model:unknown');
  assert.equal(groupKey(null), 'model:unknown');
});

test('normal and empty responses are left alone', () => {
  assert.equal(classify(answered())[0], 'answered');
  assert.deepEqual(refusals(answered()), []);
  assert.deepEqual(refusals(null), []);
  assert.equal(classify({ status: 'completed', output: [] })[0], 'empty-answer');
  assert.equal(refusalRate([]).size, 0);
  assert.equal(refusalRate(null).size, 0);
});
