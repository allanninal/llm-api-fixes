import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, errorFields, headroom, stalled } from './openai-quota-wall-audit.mjs';

const NOW = new Date('2026-08-30T12:00:00Z');
const hoursAgo = (h) => Math.floor(NOW.getTime() / 1000 - h * 3600);

const bucket = (h, requests = 10, output = 4000) => ({
  start_time: hoursAgo(h),
  results: [{ num_model_requests: requests, input_tokens: 900, output_tokens: output }],
});

const openaiError = (code) => ({
  error: { message: 'You exceeded your current quota.', type: 'insufficient_quota', code },
});

test('error fields reads nested and bare envelopes', () => {
  assert.equal(errorFields(openaiError('insufficient_quota'))[0], 'insufficient_quota');
  assert.equal(errorFields({ code: 'rate_limit_exceeded' })[0], 'rate_limit_exceeded');
  assert.deepEqual(errorFields(null), ['', '', '']);
  assert.equal(errorFields({ error: 'a string, not an object' })[0], '');
});

test('the whole point: two 429s that are not the same thing', () => {
  const [wall, wallDetail] = classify(429, openaiError('insufficient_quota'));
  const [throttle] = classify(429, openaiError('rate_limit_exceeded'));
  assert.equal(wall, 'wall');
  assert.equal(throttle, 'throttle');
  assert.match(wallDetail, /RateLimitError/);
});

test('every billing code is a wall with its own remedy', () => {
  const remedies = new Set();
  for (const code of ['credit_balance_exhausted', 'organization_spend_limit_exceeded',
    'project_spend_limit_exceeded', 'organization_usage_limit_exceeded']) {
    const [state, detail] = classify(429, openaiError(code));
    assert.equal(state, 'wall', code);
    remedies.add(detail);
  }
  assert.equal(remedies.size, 4);
});

test('an unrecognised 429 code is not retried blindly', () => {
  const [state, detail] = classify(429, openaiError('some_new_code_2027'));
  assert.equal(state, 'unclassified-429');
  assert.match(detail, /not retryable/);
});

test('a 429 with no code at all is still not a free retry loop', () => {
  assert.equal(classify(429, { error: { message: 'Too many requests' } })[0],
    'unclassified-429');
});

test('anthropic 429 matches on type because it has no code', () => {
  const [state] = classify(429, {
    type: 'error',
    error: { type: 'rate_limit_error', message: 'Number of requests has exceeded' },
  });
  assert.equal(state, 'throttle');
});

test('anthropic puts the same wall behind a 400', () => {
  const [state, detail] = classify(400, {
    error: {
      type: 'invalid_request_error',
      message: 'Your credit balance is too low to access the Claude API.',
    },
  });
  assert.equal(state, 'wall');
  assert.match(detail, /400/);
});

test('auth and server errors are not confused with either', () => {
  assert.equal(classify(401, {})[0], 'auth');
  assert.equal(classify(503, {})[0], 'transient');
  assert.equal(classify(404, {})[0], 'other');
});

test('headroom forecasts the one wall that can be forecast', () => {
  assert.equal(headroom(120, null)[0], 'tier-unknown');
  assert.equal(headroom(120, 1000)[0], 'clear');
  assert.equal(headroom(850, 1000)[0], 'approaching');
  assert.equal(headroom(1000, 1000)[0], 'at-ceiling');
});

test('stalled reads a cliff against the clock it is given', () => {
  assert.equal(stalled([bucket(30), bucket(2)], NOW)[0], 'flowing');
  const [state, detail] = stalled([bucket(30), bucket(20)], NOW);
  assert.equal(state, 'cliff');
  assert.match(detail, /20\.0 hour\(s\) ago/);
});

test('requests with no output is a different finding from a cliff', () => {
  const [state] = stalled([bucket(20, 40, 0), bucket(1)], NOW);
  assert.equal(state, 'failing-before-generation');
});

test('empty and silent windows do not claim a wall', () => {
  assert.equal(stalled([], NOW)[0], 'no-data');
  assert.equal(stalled([bucket(3, 0, 0)], NOW)[0], 'no-data');
});
