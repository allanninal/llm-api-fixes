import { test } from 'node:test';
import assert from 'node:assert/strict';
import { batchOverflows, budget, countBody, turnsRemaining, verdict, windowOf }
  from './anthropic-context-preflight.mjs';

test('input fits but the reservation does not', () => {
  assert.equal(verdict(190000, 0, 200000)[0], 'window-tight');
  const [state, detail] = verdict(190000, 16000, 200000);
  assert.equal(state, 'budget-over-window');
  assert.match(detail, /190000 input \+ 16000 max_tokens = 206000 of a 200000 token window/);
  assert.match(detail, /model_context_window_exceeded/);
  assert.match(detail, /200/);
});

test('input alone over the window is the other failure', () => {
  const [state, detail] = verdict(260000, 4000, 200000);
  assert.equal(state, 'input-over-window');
  assert.match(detail, /prompt is too long/);
  assert.equal(budget(260000, 4000), 264000);
});

test('a comfortable payload is not a finding', () => {
  const [state, detail] = verdict(40000, 8000, 200000);
  assert.equal(state, 'fits');
  assert.match(detail, /\(24%\)/);
});

test('the counting endpoint only gets the keys it accepts', () => {
  const trimmed = countBody({
    model: 'claude-sonnet-5', system: 's', messages: [], tools: [{ name: 't' }],
    tool_choice: { type: 'auto' }, thinking: { type: 'enabled' },
    max_tokens: 16000, temperature: 0.2, stream: true, service_tier: 'auto',
  });
  assert.deepEqual(Object.keys(trimmed).sort(),
    ['messages', 'model', 'system', 'thinking', 'tool_choice', 'tools']);
  assert.deepEqual(countBody(null), {});
});

test('a missing window is not an infinite one', () => {
  assert.equal(windowOf({ id: 'claude-sonnet-5', max_input_tokens: 200000 }), 200000);
  assert.equal(windowOf({ id: 'claude-sonnet-5' }), null);
  assert.equal(windowOf({ max_input_tokens: 0 }), null);
  assert.equal(windowOf({ max_input_tokens: '200000' }), null);
  assert.equal(windowOf(null), null);
  const [state, detail] = verdict(500000, 8000, null);
  assert.equal(state, 'window-unknown');
  assert.match(detail, /no max_input_tokens/);
});

test('turnsRemaining is the number a product team wants', () => {
  assert.equal(turnsRemaining(120000, 16000, 200000, 1800), 35);
  assert.equal(turnsRemaining(199000, 16000, 200000, 1800), 0);
  assert.equal(turnsRemaining(120000, 16000, null, 1800), null);
  assert.equal(turnsRemaining(120000, 16000, 200000, 0), null);
});

test('batch results yield both shapes keyed by custom_id', () => {
  const lines = [
    '{"custom_id": "doc-9", "result": {"type": "succeeded", "message": {"stop_reason": "model_context_window_exceeded"}}}',
    '{"custom_id": "doc-3", "result": {"type": "errored", "error": {"type": "invalid_request_error", "message": "prompt is too long: 412000 tokens > 200000 maximum"}}}',
    '{"custom_id": "doc-1", "result": {"type": "succeeded", "message": {"stop_reason": "end_turn"}}}',
    '',
    'not json at all',
  ];
  assert.deepEqual(batchOverflows(lines),
    { 'doc-9': 'truncated-with-200', 'doc-3': 'rejected-with-400' });
  assert.deepEqual(batchOverflows([]), {});
  assert.deepEqual(batchOverflows(null), {});
});
