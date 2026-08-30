import { test } from 'node:test';
import assert from 'node:assert/strict';
import { amount, bucketOf, byBucket, topModel, verdict }
  from './anthropic-output-cost-audit.mjs';

// amount arrives as a decimal STRING on this endpoint, not a number.
const cost = (tokenType, value, description = 'Claude Sonnet 5') => ({
  currency: 'USD', amount: String(value), token_type: tokenType, description,
  cost_type: 'tokens',
});

const costDay = (...rows) => ({ starting_at: '2026-08-01T00:00:00Z', results: rows });
const usageDay = (...rows) => ({ starting_at: '2026-08-01T00:00:00Z', results: rows });

test('amount is a string on this endpoint', () => {
  assert.equal(amount({ amount: '12.34' }), 12.34);
  assert.equal(amount({ amount: 12.34 }), 12.34);
  assert.equal(amount({ amount: '' }), 0);
  assert.equal(amount({}), 0);
  assert.equal(amount({ amount: 'n/a' }), 0);
});

test('token types fold into buckets by shape, not by exact name', () => {
  assert.equal(bucketOf('output_tokens'), 'output');
  assert.equal(bucketOf('uncached_input_tokens'), 'input');
  assert.equal(bucketOf('cache_read_input_tokens'), 'cache_read');
  assert.equal(bucketOf('cache_creation_input_tokens'), 'cache_write');
  assert.equal(bucketOf('1h_cache_creation_input_tokens'), 'cache_write');
  assert.equal(bucketOf('some_future_tier_tokens'), 'other');
  assert.equal(bucketOf(null), 'other');
});

test('unrecognised types stay in the denominator', () => {
  const rows = byBucket([costDay(cost('output_tokens', '60'),
    cost('some_future_tier_tokens', '40'))]);
  assert.equal(rows.other, 40);
  const [state, detail] = verdict(rows);
  assert.match(detail, /unrecognised 40%/);
  assert.equal(state, 'output-led');
});

test('the same spend split three ways gives three different repairs', () => {
  const outputHeavy = byBucket([costDay(cost('output_tokens', '800'),
    cost('uncached_input_tokens', '200'))]);
  const inputHeavy = byBucket([costDay(cost('output_tokens', '300'),
    cost('uncached_input_tokens', '500'), cost('cache_read_input_tokens', '200'))]);
  const even = byBucket([costDay(cost('output_tokens', '450'),
    cost('uncached_input_tokens', '550'))]);

  assert.equal(verdict(outputHeavy)[0], 'output-dominated');
  assert.equal(verdict(inputHeavy)[0], 'input-dominated');
  assert.equal(verdict(even)[0], 'balanced');
});

test('an output dominated bill names the only lever there is', () => {
  const rows = byBucket([costDay(cost('output_tokens', '800'),
    cost('uncached_input_tokens', '200'))]);
  const [, detail] = verdict(rows);
  assert.match(detail, /no caching discount/);
  assert.match(detail, /5x input/);
});

test('cache writes without reads is its own finding', () => {
  const rows = byBucket([costDay(cost('cache_creation_input_tokens', '400'),
    cost('cache_read_input_tokens', '50'), cost('output_tokens', '300'),
    cost('uncached_input_tokens', '250'))]);
  const [state, detail] = verdict(rows);
  assert.equal(state, 'cache-write-heavy');
  assert.match(detail, /amortise/);
});

test('output between half and seventy percent is not an emergency', () => {
  const rows = byBucket([costDay(cost('output_tokens', '550'),
    cost('uncached_input_tokens', '450'))]);
  assert.equal(verdict(rows)[0], 'output-led');
});

test('a quiet window reports nothing rather than a noisy share', () => {
  assert.equal(verdict(byBucket([costDay(cost('output_tokens', '0.10'))]))[0], 'no-spend');
  assert.equal(verdict(byBucket([]))[0], 'no-spend');
});

test('top model names where an effort change would land', () => {
  const [model, share] = topModel([
    usageDay({ model: 'claude-opus-5', output_tokens: 900, uncached_input_tokens: 4000 },
      { model: 'claude-sonnet-5', output_tokens: 100, uncached_input_tokens: 8000 }),
  ]);
  assert.equal(model, 'claude-opus-5');
  assert.equal(Number(share.toFixed(2)), 0.9);
  assert.deepEqual(topModel([]), [null, 0]);
});
