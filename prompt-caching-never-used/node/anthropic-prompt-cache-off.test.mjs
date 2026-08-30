import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  accumulate, cacheSavingCeiling, verdict, windowStart,
} from './anthropic-prompt-cache-off.mjs';

test('accumulate reads the nested cache_creation object', () => {
  const total = accumulate([{
    uncached_input_tokens: 100,
    cache_read_input_tokens: 40,
    cache_creation: { ephemeral_5m_input_tokens: 7, ephemeral_1h_input_tokens: 3 },
  }]);
  assert.deepEqual(total, { uncached: 100, cache_read: 40, write_5m: 7, write_1h: 3 });
});

test('accumulate treats absent and null fields as zero', () => {
  assert.equal(accumulate([{ uncached_input_tokens: null }]).uncached, 0);
  assert.equal(accumulate([{}]).write_5m, 0);
  assert.equal(accumulate(null).cache_read, 0);
});

test('accumulate adds into a running total', () => {
  const first = accumulate([{ uncached_input_tokens: 10 }]);
  assert.equal(accumulate([{ uncached_input_tokens: 5 }], first).uncached, 15);
});

test('zero reads and zero writes on real traffic is the finding', () => {
  const [state, detail] = verdict({
    uncached: 50_000_000, cache_read: 0, write_5m: 0, write_1h: 0,
  });
  assert.equal(state, 'never-used');
  assert.match(detail, /never been switched on/);
});

test('writes without reads is the other note not this one', () => {
  const [state, detail] = verdict({
    uncached: 50_000_000, cache_read: 0, write_5m: 4_000_000, write_1h: 0,
  });
  assert.equal(state, 'writes-only');
  assert.match(detail, /more than leaving it off/);
});

test('any read at all means caching is on', () => {
  assert.equal(verdict({ uncached: 5_000_000, cache_read: 1, write_5m: 0, write_1h: 0 })[0],
               'in-use');
});

test('a quiet workload makes no claim either way', () => {
  assert.equal(verdict({ uncached: 900, cache_read: 0, write_5m: 0, write_1h: 0 })[0],
               'too-little-traffic');
});

test('the saving ceiling prices the reusable share at the read rate', () => {
  assert.equal(cacheSavingCeiling(1_000_000, 1.0), 900_000);
  assert.equal(cacheSavingCeiling(1_000_000, 0.5), 450_000);
  assert.equal(cacheSavingCeiling(1_000_000, 0.0), 0);
});

test('the ceiling refuses a fraction that is not a fraction', () => {
  assert.throws(() => cacheSavingCeiling(1_000_000, 1.4), RangeError);
});

test('the window start is floored to midnight UTC', () => {
  assert.equal(windowStart(7, new Date('2026-08-30T13:45:12Z')), '2026-08-23T00:00:00Z');
});
