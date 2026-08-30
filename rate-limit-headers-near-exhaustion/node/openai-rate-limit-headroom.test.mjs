import { test } from 'node:test';
import assert from 'node:assert/strict';
import { binding, headroom, parseCount, parseReset, scopeNote, triples, verdict }
  from './openai-rate-limit-headroom.mjs';

test('token headroom is the finding while requests look fine', () => {
  const parsed = triples({
    'X-RateLimit-Limit-Requests': '10000',
    'X-RateLimit-Remaining-Requests': '9100',
    'X-RateLimit-Reset-Requests': '6m0s',
    'x-ratelimit-limit-tokens': '200000',
    'x-ratelimit-remaining-tokens': '8000',
    'x-ratelimit-reset-tokens': '47s',
  });
  assert.equal(verdict('requests', parsed.requests)[0], 'headroom');
  const [state, detail] = verdict('tokens', parsed.tokens);
  assert.equal(state, 'near-exhaustion');
  assert.match(detail, /8000 of 200000 left \(4%\), resets in 47s/);
  assert.deepEqual(binding(parsed), ['tokens', 0.04]);
});

test('an empty bucket is reported before any 429 arrives', () => {
  const [state, detail] = verdict('tokens', { limit: 200000, remaining: 0, reset: 12 });
  assert.equal(state, 'exhausted');
  assert.match(detail, /empty now/);
});

test('absent headers are not an empty bucket', () => {
  assert.equal(parseCount(null), null);
  assert.equal(parseCount(''), null);
  assert.equal(parseCount('0'), 0);
  assert.equal(parseCount('1,500,000'), 1500000);
  assert.equal(parseCount('not a number'), null);
  assert.equal(headroom({ limit: 200000, remaining: null }), null);
  assert.equal(verdict('tokens', { limit: 200000, remaining: null })[0], 'unreadable');
});

test('go duration resets parse and ms is not minutes', () => {
  assert.equal(parseReset('500ms'), 0.5);
  assert.equal(parseReset('6m0s'), 360);
  assert.equal(parseReset('1h2m3s'), 3723);
  assert.equal(parseReset('47s'), 47);
  assert.equal(parseReset('60 seconds'), null);
  assert.equal(parseReset('soon'), null);
  assert.equal(parseReset(''), null);
  assert.equal(parseReset(null), null);
});

test('a probe with no rate limit headers parses to nothing', () => {
  assert.deepEqual(triples({ 'content-type': 'application/json' }), {});
  assert.deepEqual(triples({}), {});
  assert.deepEqual(triples(null), {});
  assert.equal(binding({}), null);
});

test('a fetch Headers object is read the same way as a plain object', () => {
  const h = new Headers({ 'x-ratelimit-limit-tokens': '100',
                          'x-ratelimit-remaining-tokens': '5' });
  assert.equal(verdict('tokens', triples(h).tokens)[0], 'near-exhaustion');
});

test('the project ceiling is the real ceiling when it is lower', () => {
  const parsed = triples({
    'x-ratelimit-limit-tokens': '200000',
    'x-ratelimit-remaining-tokens': '150000',
    'x-ratelimit-limit-project-tokens': '150000',
    'x-ratelimit-remaining-project-tokens': '12000',
    'x-ratelimit-reset-project-tokens': '30s',
  });
  assert.deepEqual(scopeNote(parsed), [['project', 'tokens', 150000, 200000]]);
  assert.equal(verdict('tokens', parsed.tokens)[0], 'headroom');
  assert.equal(verdict('project-tokens', parsed['project-tokens'])[0], 'near-exhaustion');
  assert.equal(binding(parsed)[0], 'project-tokens');
});

test('scopeNote says nothing when there is nothing to compare', () => {
  assert.deepEqual(scopeNote(triples({
    'x-ratelimit-limit-tokens': '200000',
    'x-ratelimit-remaining-tokens': '150000',
  })), []);
  assert.deepEqual(scopeNote({}), []);
  assert.deepEqual(scopeNote(null), []);
});
