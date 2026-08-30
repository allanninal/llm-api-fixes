import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  classify, declaredTools, functionCalls, parseArguments, repairLines,
  schemaViolations, wasTruncated,
} from './openai-tool-call-arguments.mjs';

const CHARGE = {
  type: 'object',
  additionalProperties: false,
  required: ['account_id', 'amount_cents', 'currency'],
  properties: {
    account_id: { type: 'string' },
    amount_cents: { type: 'integer' },
    currency: { type: 'string', enum: ['usd', 'eur'] },
  },
};

const response = (args, { name = 'charge', strict = true,
  status = 'completed' } = {}) => ({
  id: 'resp_t', status,
  tools: [{ type: 'function', name: 'charge', parameters: CHARGE, strict }],
  output: [{ type: 'function_call', name, call_id: 'call_1', arguments: args }],
});

test('arguments that parse and still break the contract', () => {
  const stored = response('{"account_id": "acct_9", "amount_cents": "1200", ' +
    '"currency": "gbp", "idempotency_key": "k1"}');
  const call = functionCalls(stored)[0];
  const [value, error] = parseArguments(call.arguments);
  assert.equal(error, null);
  assert.equal(typeof value, 'object');

  const [state, detail] = classify(call, declaredTools(stored));
  assert.equal(state, 'arguments-violate-schema');
  assert.match(detail, /amount_cents: expected integer, got string/);
  assert.match(detail, /currency: "gbp" is not one of the 2 declared value\(s\)/);
  assert.match(detail, /idempotency_key: not declared/);
  assert.match(repairLines(state)[0], /feed the validation error back to the model/);
});

test('a missing required argument is found before the handler is called', () => {
  const stored = response('{"account_id": "acct_9", "currency": "usd"}');
  const [state, detail] = classify(functionCalls(stored)[0], declaredTools(stored));
  assert.equal(state, 'arguments-violate-schema');
  assert.match(detail, /arguments\.amount_cents: required and missing/);
});

test('a cut argument string belongs to the truncation note', () => {
  const stored = response('{"account_id": "acct_9", "amount_cent',
    { status: 'incomplete' });
  stored.incomplete_details = { reason: 'max_output_tokens' };
  assert.equal(wasTruncated(stored), true);
  const [state, detail] = classify(functionCalls(stored)[0],
    declaredTools(stored), wasTruncated(stored));
  assert.equal(state, 'arguments-truncated');
  assert.match(detail, /cut mid-write rather than written wrongly/);
  assert.match(repairLines(state)[0], /Not a schema problem/);
});

test('a broken string on a completed response is the model own work', () => {
  const stored = response('{{"account_id": "acct_9"}}');
  assert.equal(wasTruncated(stored), false);
  const [state, detail] = classify(functionCalls(stored)[0], declaredTools(stored));
  assert.equal(state, 'arguments-unparseable');
  assert.match(detail, /nothing was constraining the grammar/);
});

test('an unknown tool name is a lookup error not a parse error', () => {
  const stored = response('{"account_id": "acct_9"}', { name: 'charge_v2' });
  const [state, detail] = classify(functionCalls(stored)[0], declaredTools(stored));
  assert.equal(state, 'unknown-tool');
  assert.match(detail, /indexes a handler map by name raises here/);
  assert.match(repairLines(state)[1], /renamed on one side only/);
});

test('a valid call is dispatchable and an unstrict one is flagged anyway', () => {
  const good = '{"account_id": "acct_9", "amount_cents": 1200, "currency": "usd"}';
  const stored = response(good);
  assert.equal(classify(functionCalls(stored)[0], declaredTools(stored))[0],
    'dispatchable');

  const loose = response(good, { strict: false });
  const [state, detail] = classify(functionCalls(loose)[0], declaredTools(loose));
  assert.equal(state, 'dispatchable-unconstrained');
  assert.match(detail, /nothing guaranteed that it would/);
});

test('the chat completions shape and the empty argument string', () => {
  const legacy = {
    choices: [{ finish_reason: 'tool_calls', message: { tool_calls: [
      { id: 'call_9', type: 'function',
        function: { name: 'ping', arguments: '' } }] } }],
    tools: [{ type: 'function', function: { name: 'ping', strict: true,
      parameters: { type: 'object', additionalProperties: false,
        properties: {}, required: [] } } }],
  };
  const call = functionCalls(legacy)[0];
  assert.equal(call.name, 'ping');
  assert.equal(call.callId, 'call_9');
  assert.deepEqual(parseArguments(''), [{}, null]);
  assert.equal(classify(call, declaredTools(legacy))[0], 'dispatchable');
});

test('the walker and the readers survive junk', () => {
  assert.equal(parseArguments(null)[1], 'the arguments field is absent');
  assert.equal(parseArguments('[1, 2]')[1], 'arguments parsed to array, not an object');
  assert.deepEqual(schemaViolations({ a: 1 }, null), []);
  assert.deepEqual(schemaViolations({ a: 1 }, {}), []);
  assert.deepEqual(schemaViolations(true, { type: 'integer' }),
    ['arguments: expected integer, got boolean']);
  assert.deepEqual(schemaViolations({ rows: [{ sku: 1 }] }, {
    type: 'object',
    properties: { rows: { type: 'array', items: { type: 'object',
      properties: { sku: { type: 'string' } } } } },
  }), ['arguments.rows[0].sku: expected string, got integer']);
  assert.deepEqual(functionCalls(null), []);
  assert.equal(declaredTools(null).size, 0);
  assert.equal(wasTruncated(null), false);
});
