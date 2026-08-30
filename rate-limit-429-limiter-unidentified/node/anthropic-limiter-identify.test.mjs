import { test } from 'node:test';
import assert from 'node:assert/strict';
import { configured, emptiest, logHeaders, mirrors, parseCount, readTriples,
         secondsUntil, shareLeft, verdict }
  from './anthropic-limiter-identify.mjs';

const NOW = new Date('2026-08-30T12:00:00Z');

test('the aggregate ceiling names the binding limiter', () => {
  const parsed = readTriples({
    'anthropic-ratelimit-requests-limit': '4000',
    'anthropic-ratelimit-requests-remaining': '3600',
    'anthropic-ratelimit-input-tokens-limit': '5000000',
    'anthropic-ratelimit-input-tokens-remaining': '4000000',
    'anthropic-ratelimit-output-tokens-limit': '400000',
    'anthropic-ratelimit-output-tokens-remaining': '12000',
    'anthropic-ratelimit-tokens-limit': '400000',
    'anthropic-ratelimit-tokens-remaining': '12000',
  });
  assert.equal(mirrors(parsed), 'output-tokens');
  assert.deepEqual(emptiest(parsed), ['output-tokens', 0.03]);
  const [state, detail] = verdict(parsed);
  assert.equal(state, 'identified');
  assert.match(detail, /output-tokens is the emptiest named bucket at 3% remaining/);
});

test('the tightest ceiling and the emptiest bucket can disagree', () => {
  const parsed = readTriples({
    'anthropic-ratelimit-requests-limit': '4000',
    'anthropic-ratelimit-requests-remaining': '40',
    'anthropic-ratelimit-input-tokens-limit': '5000000',
    'anthropic-ratelimit-input-tokens-remaining': '4900000',
    'anthropic-ratelimit-output-tokens-limit': '400000',
    'anthropic-ratelimit-output-tokens-remaining': '380000',
    'anthropic-ratelimit-tokens-limit': '400000',
    'anthropic-ratelimit-tokens-remaining': '380000',
  });
  const [state, detail] = verdict(parsed);
  assert.equal(state, 'disagreement');
  assert.match(detail, /requests is the emptiest named bucket at 1% remaining/);
  assert.match(detail, /mirrors output-tokens/);
});

test('an aggregate matching neither ceiling is a third limit', () => {
  const parsed = readTriples({
    'anthropic-ratelimit-requests-limit': '4000',
    'anthropic-ratelimit-requests-remaining': '3900',
    'anthropic-ratelimit-input-tokens-limit': '5000000',
    'anthropic-ratelimit-input-tokens-remaining': '4900000',
    'anthropic-ratelimit-output-tokens-limit': '400000',
    'anthropic-ratelimit-output-tokens-remaining': '390000',
    'anthropic-ratelimit-tokens-limit': '150000',
    'anthropic-ratelimit-tokens-remaining': '150000',
  });
  assert.equal(mirrors(parsed), 'unmatched');
  assert.equal(verdict(parsed)[0], 'aggregate-unmatched');
});

test('no headers is a finding and not a pass', () => {
  assert.deepEqual(readTriples({ 'content-type': 'application/json' }), {});
  assert.deepEqual(readTriples(null), {});
  const [state, detail] = verdict({});
  assert.equal(state, 'headers-missing');
  assert.match(detail, /retry-after would be missing too/);
  assert.deepEqual(logHeaders({ 'content-type': 'application/json' }), []);
});

test('the aggregate never competes to be the emptiest bucket', () => {
  const parsed = {
    requests: { limit: 100, remaining: 90 },
    'output-tokens': { limit: 1000, remaining: 500 },
    tokens: { limit: 1000, remaining: 1 },
  };
  assert.deepEqual(emptiest(parsed), ['output-tokens', 0.5]);
});

test('absent and empty are different readings', () => {
  assert.equal(parseCount(null), null);
  assert.equal(parseCount(''), null);
  assert.equal(parseCount('0'), 0);
  assert.equal(parseCount('2,000,000'), 2000000);
  assert.equal(parseCount('lots'), null);
  assert.equal(shareLeft({ limit: 100, remaining: null }), null);
  assert.equal(shareLeft({ limit: 0, remaining: 0 }), null);
  assert.equal(verdict({ requests: { limit: null, remaining: null } })[0], 'unreadable');
});

test('rfc3339 resets parse and unreadable ones stay unreadable', () => {
  assert.equal(secondsUntil('2026-08-30T12:00:30Z', NOW), 30);
  assert.equal(secondsUntil('2026-08-30T12:00:30+00:00', NOW), 30);
  assert.equal(secondsUntil('in a bit', NOW), null);
  assert.equal(secondsUntil('', NOW), null);
  assert.equal(secondsUntil(null, NOW), null);
});

test('an unpublished limiter is not an unlimited one', () => {
  const folded = configured({ data: [
    { model_group: 'claude-sonnet-5', limits: [
      { type: 'requests_per_minute', value: 4000 },
      { type: 'input_tokens_per_minute', value: 5000000 },
      { type: 'output_tokens_per_minute', value: 1000000 }] },
    { model_group: 'message-batches', limits: [
      { type: 'requests_per_minute', value: 100 }] },
  ] });
  assert.equal(folded['claude-sonnet-5'].output_tokens_per_minute, 1000000);
  assert.equal(folded['message-batches'].input_tokens_per_minute, null);
  assert.equal(folded['message-batches'].requests_per_minute, 100);
  assert.deepEqual(configured({}), {});
  assert.deepEqual(configured(null), {});
});

test('the repair lists only headers that actually arrived', () => {
  assert.deepEqual(logHeaders({
    'Anthropic-RateLimit-Output-Tokens-Remaining': '12000',
    'anthropic-ratelimit-tokens-limit': '400000',
    'Retry-After': '12',
    'request-id': 'req_fake123',
    'content-type': 'application/json',
  }), ['anthropic-ratelimit-output-tokens-remaining',
       'anthropic-ratelimit-tokens-limit',
       'request-id', 'retry-after']);
});
