import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  accumulate, breakEvenRatio, effectiveMultiplier, verdict, windowStart,
} from './anthropic-cache-write-ratio.mjs';

test('accumulate keeps the two TTLs apart', () => {
  const total = accumulate([{
    cache_read_input_tokens: 5,
    cache_creation: { ephemeral_5m_input_tokens: 100, ephemeral_1h_input_tokens: 20 },
  }]);
  assert.equal(total.write_5m, 100);
  assert.equal(total.write_1h, 20);
  assert.equal(total.cache_read, 5);
});

test('break-even for pure 5m writes', () => {
  assert.equal(Number(breakEvenRatio(1000, 0).toFixed(4)), 0.2778);
});

test('break-even for pure 1h writes is about four times higher', () => {
  assert.equal(Number(breakEvenRatio(0, 1000).toFixed(4)), 1.1111);
});

test('break-even of nothing written is null not zero', () => {
  assert.equal(breakEvenRatio(0, 0), null);
});

test('at break-even the effective multiplier is exactly one', () => {
  for (const [w5, w1h] of [[1000, 0], [0, 1000], [600, 400]]) {
    const reads = breakEvenRatio(w5, w1h) * (w5 + w1h);
    assert.equal(Number(effectiveMultiplier(w5, w1h, reads).toFixed(6)), 1);
  }
});

test('writes with no reads cost more than not caching', () => {
  assert.equal(effectiveMultiplier(1000, 0, 0), 1.25);
  assert.equal(effectiveMultiplier(0, 1000, 0), 2.0);
});

test('a key that writes and never reads is losing', () => {
  const [state, detail] = verdict({ cache_read: 0, write_5m: 5_000_000, write_1h: 0 });
  assert.equal(state, 'losing');
  assert.match(detail, /1\.25x/);
});

test('a key reading back many times is paying off', () => {
  assert.equal(
    verdict({ cache_read: 50_000_000, write_5m: 5_000_000, write_1h: 0 })[0],
    'paying-off');
});

test('just above break-even is marginal not safe', () => {
  const writes = 5_000_000;
  const reads = Math.floor(breakEvenRatio(writes, 0) * writes * 1.1);
  assert.equal(verdict({ cache_read: reads, write_5m: writes, write_1h: 0 })[0],
               'marginal');
});

test('no writes and no reads is the other note', () => {
  const [state, detail] = verdict({ cache_read: 0, write_5m: 0, write_1h: 0 });
  assert.equal(state, 'no-caching');
  assert.match(detail, /different problem/);
});

test('reads with no writes in the window is not a ratio', () => {
  const [state, detail] = verdict({ cache_read: 9_000_000, write_5m: 0, write_1h: 0 });
  assert.equal(state, 'reads-only');
  assert.match(detail, /Widen the window/);
});

test('a trickle of writes makes no claim', () => {
  assert.equal(verdict({ cache_read: 0, write_5m: 10, write_1h: 0 })[0],
               'too-little-traffic');
});

test('the window start is floored to the hour', () => {
  assert.equal(windowStart(7, new Date('2026-08-30T13:45:12Z')), '2026-08-23T13:00:00Z');
});
