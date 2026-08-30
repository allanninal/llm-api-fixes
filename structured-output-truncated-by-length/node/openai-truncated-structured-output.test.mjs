import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  batchLineVerdict, ceilingUse, classify, incompleteReason, jsonState,
  outputText, reasoningShare, repairLines,
} from './openai-truncated-structured-output.mjs';

const stored = (text, opts = {}) => {
  const body = {
    id: 'resp_1',
    status: opts.status ?? 'completed',
    output: [{ type: 'message', content: [{ type: 'output_text', text }] }],
  };
  if (opts.reason) body.incomplete_details = { reason: opts.reason };
  if (opts.cap !== undefined) body.max_output_tokens = opts.cap;
  if (opts.used !== undefined) {
    body.usage = { output_tokens: opts.used };
    if (opts.reasoning !== undefined) {
      body.usage.output_tokens_details = { reasoning_tokens: opts.reasoning };
    }
  }
  return body;
};

test('an incomplete response holding a json prefix is the whole note', () => {
  const half = '{"invoice_id": "INV-8817", "lines": [{"sku": "AB-1", "note": "part';
  const response = stored(half, {
    status: 'incomplete', reason: 'max_output_tokens', cap: 1024, used: 1024 });
  assert.equal(incompleteReason(response), 'max_output_tokens');
  assert.equal(jsonState(half), 'truncated');
  assert.equal(ceilingUse(response), 1);

  const [state, detail] = classify(response);
  assert.equal(state, 'truncated-by-length');
  assert.match(detail, /valid prefix that never closes/);
  const repairs = repairLines(state, response);
  assert.match(repairs[0], /incomplete_details\.reason/);
  assert.match(repairs[1], /1024 output tokens/);
});

test('a ceiling eaten by reasoning gets its own state', () => {
  const response = stored('', {
    status: 'incomplete', reason: 'max_output_tokens',
    cap: 2000, used: 2000, reasoning: 1900 });
  assert.equal(reasoningShare(response), 0.95);
  const [state, detail] = classify(response);
  assert.equal(state, 'ceiling-spent-on-reasoning');
  assert.match(detail, /visible answer barely started/);
  assert.match(repairLines(state, response).join(' '), /reasoning effort/);
});

test('jsonState separates a cut document from a wrong one', () => {
  assert.equal(jsonState('{"a": 1}'), 'parses');
  assert.equal(jsonState('{"a": [1, 2,'), 'truncated');
  assert.equal(jsonState('{"a": "unter'), 'truncated');
  assert.equal(jsonState('{"a": "esc\\\\'), 'truncated');
  assert.equal(jsonState('{"a": 1,}'), 'malformed');
  assert.equal(jsonState('Sorry, I cannot help with that.'), 'malformed');
  assert.equal(jsonState('   '), 'empty');
  assert.equal(jsonState(null), 'empty');
});

test('a refusal and a filter stop are handed to the other note', () => {
  const refusal = { status: 'completed', output: [{ type: 'message',
    content: [{ type: 'refusal', refusal: "I can't help with that." }] }] };
  const [state, detail] = classify(refusal);
  assert.equal(state, 'refused');
  assert.match(detail, /Nothing was cut/);

  const filtered = stored('', { status: 'incomplete', reason: 'content_filter' });
  assert.equal(classify(filtered)[0], 'stopped-by-filter');
  assert.match(classify(filtered)[1], /refusal note/);
});

test('a completed response that still fails to parse is not this note', () => {
  const [state, detail] = classify(stored('{"total": 12,}'));
  assert.equal(state, 'schema-not-followed');
  assert.match(detail, /advisory schema/);
  assert.equal(classify(stored('{"total": 12}'))[0], 'complete');
  assert.equal(classify(stored('{"total": 12,'))[0], 'cut-without-a-reason');
});

test('chat completions rows are read as well as responses rows', () => {
  const legacy = { choices: [{ finish_reason: 'length',
    message: { content: '{"rows": [{"id": 1' } }] };
  assert.equal(outputText(legacy), '{"rows": [{"id": 1');
  assert.equal(incompleteReason(legacy), 'max_output_tokens');
  assert.equal(classify(legacy)[0], 'truncated-by-length');
});

test('a missing ceiling is not a ceiling of zero', () => {
  assert.equal(ceilingUse(stored('{}')), null);
  assert.equal(ceilingUse(stored('{}', { cap: 0, used: 0 })), null);
  assert.equal(ceilingUse(null), null);
  assert.equal(reasoningShare(stored('{}', { cap: 10, used: 0 })), null);
  assert.equal(classify(null)[0], 'empty-output');
});

test('batch results are keyed by custom_id and read line by line', () => {
  const cut = JSON.stringify({ custom_id: 'row-9', result: {
    type: 'succeeded',
    message: { stop_reason: 'max_tokens', usage: { output_tokens: 4096 },
      content: [{ type: 'text', text: '{"a": 1' }] } } });
  assert.deepEqual(batchLineVerdict(cut).slice(0, 2), ['row-9', 'truncated-by-length']);
  assert.match(batchLineVerdict(cut)[2], /4096/);

  const tool = JSON.stringify({ custom_id: 'row-10', result: {
    type: 'succeeded',
    message: { stop_reason: 'max_tokens',
      content: [{ type: 'tool_use', name: 'charge', input: {} }] } } });
  assert.equal(batchLineVerdict(tool)[1], 'truncated-tool-use');
  assert.match(batchLineVerdict(tool)[2], /cannot be executed/);

  const done = JSON.stringify({ custom_id: 'row-11', result: {
    type: 'succeeded', message: { stop_reason: 'end_turn', content: [] } } });
  assert.equal(batchLineVerdict(done)[1], 'complete');
  const errored = JSON.stringify({ custom_id: 'row-12', result: { type: 'errored' } });
  assert.equal(batchLineVerdict(errored)[1], 'not-succeeded');
  assert.equal(batchLineVerdict('{not json')[1], 'unreadable');
  assert.equal(batchLineVerdict('')[1], 'unreadable');
});
