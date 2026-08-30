import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  classify, declaredFormat, looseTools, repairLines, schemaBlockers, schemaSize,
  strictState,
} from './openai-advisory-schema.mjs';

const TIGHT = {
  type: 'object',
  additionalProperties: false,
  required: ['invoice_id', 'total'],
  properties: { invoice_id: { type: 'string' }, total: { type: 'number' } },
};

const response = (format, tools) => {
  const body = { id: 'resp_s', status: 'completed', model: 'gpt-5.1',
    text: { format },
    output: [{ type: 'message', content: [{ type: 'output_text',
      text: '{"invoice_id": "INV-1", "total": 1}' }] }] };
  if (tools !== undefined) body.tools = tools;
  return body;
};

test('a schema without strict is advice and the json still parsed', () => {
  const stored = response({ type: 'json_schema', name: 'invoice', schema: TIGHT });
  assert.deepEqual(declaredFormat(stored).slice(0, 3), ['json_schema', 'invoice', null]);
  assert.equal(strictState('json_schema', null), 'advisory');

  const [state, detail] = classify(stored);
  assert.equal(state, 'advisory-schema');
  assert.match(detail, /strict absent/);
  assert.match(detail, /wrong shape is a legal outcome/);
  assert.match(repairLines(stored, state).join(' '), /one-line change/);
});

test('strict false reads the same as strict missing', () => {
  const stored = response({ type: 'json_schema', name: 'invoice',
    strict: false, schema: TIGHT });
  const [state, detail] = classify(stored);
  assert.equal(state, 'advisory-schema');
  assert.match(detail, /strict false/);
});

test('legacy json_object mode is named as its own thing', () => {
  const stored = response({ type: 'json_object' });
  const [state, detail] = classify(stored);
  assert.equal(state, 'no-schema');
  assert.match(detail, /valid JSON and nothing else/);
  assert.match(repairLines(stored, state)[0], /json_object to a json_schema/);
});

test('schemaBlockers names every rule the subset requires', () => {
  const loose = {
    type: 'object',
    required: ['invoice_id'],
    properties: {
      invoice_id: { type: 'string', minLength: 3 },
      note: { type: 'string' },
      lines: { type: 'array',
        items: { type: 'object', additionalProperties: false,
          properties: { sku: { type: 'string' } }, required: ['sku'] } },
    },
  };
  const found = schemaBlockers(loose).join(' | ');
  assert.match(found, /\$: needs additionalProperties: false/);
  assert.match(found, /missing lines, note/);
  assert.match(found, /minLength are silently unenforced/);
  assert.ok(!found.includes('$.lines[]'));
  assert.deepEqual(schemaBlockers(TIGHT), []);
});

test('a root that is not a plain object cannot be strict at all', () => {
  assert.match(schemaBlockers({ anyOf: [TIGHT, { type: 'object' }] })[0],
    /root may not be anyOf/);
  assert.match(schemaBlockers({ type: 'array', items: TIGHT })[0],
    /root type must be object, not array/);
  assert.match(schemaBlockers(null)[0], /not a schema object/);
});

test('depth beyond five levels is reported and the walk stops', () => {
  let schema = { type: 'object', additionalProperties: false,
    required: ['a'], properties: { a: { type: 'string' } } };
  for (let i = 0; i < 6; i += 1) {
    schema = { type: 'object', additionalProperties: false,
      required: ['child'], properties: { child: schema } };
  }
  assert.ok(schemaBlockers(schema).some((f) => f.includes('past the limit of 5')));
  assert.ok(schemaSize(schema).depth > 5);
});

test('a strict format beside a loose tool is still a gap', () => {
  const tools = [
    { type: 'function', name: 'charge', parameters: TIGHT, strict: true },
    { type: 'function', name: 'refund', parameters: TIGHT },
  ];
  const stored = response({ type: 'json_schema', name: 'invoice',
    strict: true, schema: TIGHT }, tools);
  assert.deepEqual(looseTools(stored), ['refund']);
  const [state, detail] = classify(stored);
  assert.equal(state, 'advisory-tools');
  assert.match(detail, /refund/);
  assert.match(repairLines(stored, state)[0], /every tool as well as on the text format/);
});

test('the chat completions shape and the clean cases', () => {
  const legacy = { response_format: { type: 'json_schema',
    json_schema: { name: 'invoice', strict: true, schema: TIGHT } } };
  assert.deepEqual(declaredFormat(legacy).slice(0, 3), ['json_schema', 'invoice', true]);
  assert.equal(classify(legacy)[0], 'enforced');
  assert.equal(classify({})[0], 'free-text');
  assert.equal(classify(null)[0], 'free-text');
  assert.deepEqual(repairLines({}, 'free-text'), []);
  assert.deepEqual(looseTools({}), []);
  assert.deepEqual(schemaSize({}), { properties: 0, depth: 1, enum: 0 });
});
