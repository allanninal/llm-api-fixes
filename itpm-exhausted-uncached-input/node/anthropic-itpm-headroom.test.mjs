import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cacheReadShare, cacheReadsCount, chargeableInput, headroomMultiplier,
         itpmByGroup, limitFor, peaks, verdict }
  from './anthropic-itpm-headroom.mjs';

/** One 1m bucket from GET /v1/organizations/usage_report/messages. */
function minute(stamp, model, { uncached = 0, write5m = 0, write1h = 0, read = 0 } = {}) {
  return { starting_at: stamp, results: [{
    model,
    uncached_input_tokens: uncached,
    cache_read_input_tokens: read,
    cache_creation: { ephemeral_5m_input_tokens: write5m,
                      ephemeral_1h_input_tokens: write1h },
    output_tokens: 12000,
  }] };
}

const stamp = (i) => `2026-08-30T14:${String(i).padStart(2, '0')}:00Z`;

test('a full input limiter with no cache reads is the finding', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-sonnet-5',
      { uncached: i === 7 ? 4880000 : 900000, read: i === 7 ? 100000 : 0 })));
  const [state, detail] = verdict('claude-sonnet-5', stats['claude-sonnet-5'], 5000000);
  assert.equal(state, 'itpm-saturated-uncached');
  assert.match(detail, /against an ITPM of 5000000 \(98%\)/);
  assert.match(detail, /cache reads were 2% of that minute's input/);
  assert.match(detail, /buys throughput and not only a discount/);
  assert.equal(headroomMultiplier(0.8).toFixed(1), '5.0');
  assert.equal(headroomMultiplier(0).toFixed(1), '1.0');
});

test('the same full ceiling with a cached prefix is a different finding', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-sonnet-5',
      { uncached: i === 3 ? 4880000 : 100000, read: i === 3 ? 19520000 : 0 })));
  const [state, detail] = verdict('claude-sonnet-5', stats['claude-sonnet-5'], 5000000);
  assert.equal(state, 'itpm-saturated-already-cached');
  assert.match(detail, /cache reads were 80% of that minute's input/);
  assert.match(detail, /limit increase/);
});

test('haiku 3.5 charges cache reads so caching buys no headroom', () => {
  assert.equal(cacheReadsCount('claude-3-5-haiku-20241022'), true);
  assert.equal(cacheReadsCount('claude-haiku-4-5-20251001'), false);
  assert.equal(cacheReadsCount('claude-opus-5'), false);

  const result = { uncached_input_tokens: 1000, cache_read_input_tokens: 4000,
                   cache_creation: { ephemeral_5m_input_tokens: 500 } };
  assert.equal(chargeableInput(result, 'claude-sonnet-5'), 1500);
  assert.equal(chargeableInput(result, 'claude-3-5-haiku-20241022'), 5500);

  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-3-5-haiku-20241022', { uncached: 200000, read: 1800000 })));
  const [state, detail] = verdict('claude-3-5-haiku-20241022',
    stats['claude-3-5-haiku-20241022'], 2000000);
  assert.equal(state, 'itpm-saturated-cache-counts');
  assert.match(detail, /buys no headroom at all/);
});

test('chargeableInput reads the nested cache_creation object', () => {
  assert.equal(chargeableInput({ uncached_input_tokens: 100,
    cache_creation: { ephemeral_5m_input_tokens: 7, ephemeral_1h_input_tokens: 3 } },
    'claude-opus-5'), 110);
  assert.equal(chargeableInput({ cache_creation_input_tokens: 999 }, 'claude-opus-5'), 0);
  assert.equal(chargeableInput({ uncached_input_tokens: null }, 'claude-opus-5'), 0);
  assert.equal(chargeableInput(null, 'claude-opus-5'), 0);
});

test('the peak minute survives an otherwise quiet window', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-opus-5', { uncached: i === 11 ? 4800000 : 200000 })));
  const row = stats['claude-opus-5'];
  assert.equal(row.peak, 4800000);
  assert.equal(row.peak_at, '2026-08-30T14:11:00Z');
  assert.equal(row.minutes, 20);
  assert.equal(verdict('claude-opus-5', row, 5000000)[0], 'itpm-saturated-uncached');
});

test('a window too short to have a peak gets no verdict', () => {
  const stats = peaks([minute('2026-08-30T14:00:00Z', 'claude-opus-5',
    { uncached: 9000000 })]);
  assert.equal(verdict('claude-opus-5', stats['claude-opus-5'], 5000000)[0],
               'too-few-buckets');
});

test('an unpublished ceiling is not an absent one', () => {
  const groups = itpmByGroup({ data: [
    { model_group: 'claude-sonnet-5', limits: [
      { type: 'input_tokens_per_minute', value: 5000000 },
      { type: 'output_tokens_per_minute', value: 1000000 }] },
    { model_group: 'claude-fable-5', limits: [
      { type: 'output_tokens_per_minute', value: 300000 }] },
  ] });
  assert.equal(groups['claude-sonnet-5'], 5000000);
  assert.equal(groups['claude-fable-5'], null);
  assert.equal(limitFor(groups, 'claude-sonnet-5-20260101'), 5000000);
  assert.equal(limitFor(groups, 'claude-fable-5'), null);
  assert.equal(limitFor(groups, 'claude-opus-5'), null);
  assert.equal(verdict('claude-fable-5', { minutes: 60, peak: 9 }, null)[0],
               'no-limit-published');
});

test('longest prefix wins when two groups could claim a model', () => {
  const groups = { 'claude-haiku': 1000, 'claude-haiku-4-5': 5000000 };
  assert.equal(limitFor(groups, 'claude-haiku-4-5-20251001'), 5000000);
  assert.equal(limitFor(groups, ''), null);
  assert.equal(limitFor({}, 'claude-opus-5'), null);
  assert.equal(cacheReadShare({ peak: 0, peak_read: 0 }, 'claude-opus-5'), null);
  assert.equal(headroomMultiplier(null), null);
});
