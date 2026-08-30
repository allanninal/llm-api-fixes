import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generated, impliedMeanOutput, limitsByGroup, limitsFor,
         outputToInputRatio, peaks, received, verdict }
  from './anthropic-otpm-ceiling.mjs';

const SONNET = { requests_per_minute: 4000,
                 input_tokens_per_minute: 5000000,
                 output_tokens_per_minute: 1000000 };

/** One 1m bucket from GET /v1/organizations/usage_report/messages. */
function minute(stamp, model, { out = 0, uncached = 0, read = 0 } = {}) {
  return { starting_at: stamp, results: [{
    model,
    output_tokens: out,
    uncached_input_tokens: uncached,
    cache_read_input_tokens: read,
    cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
  }] };
}

const stamp = (i) => `2026-08-30T14:${String(i).padStart(2, '0')}:00Z`;

test('a full output limiter beside a comfortable input one is the finding', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-opus-5',
      { out: i === 5 ? 980000 : 200000, uncached: i === 5 ? 1200000 : 400000 })));
  const [state, detail] = verdict('claude-opus-5', stats['claude-opus-5'], SONNET);
  assert.equal(state, 'otpm-saturated');
  assert.match(detail, /generated 980000 of an OTPM of 1000000 \(98%\)/);
  assert.match(detail, /while input sat at 24% of ITPM/);
  assert.match(detail, /no cached output/);
  assert.equal(Math.round(impliedMeanOutput(980000, 4000)), 245);
  assert.equal(Math.round(outputToInputRatio(SONNET) * 100), 20);
});

test('a full input limiter is handed to the other note', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-sonnet-5',
      { out: i === 9 ? 100000 : 20000, uncached: i === 9 ? 4900000 : 300000 })));
  const [state, detail] = verdict('claude-sonnet-5', stats['claude-sonnet-5'], SONNET);
  assert.equal(state, 'input-bound');
  assert.match(detail, /input limiter is the one that is full here/);
});

test('both limiters full is volume rather than shape', () => {
  const stats = peaks([...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-sonnet-5', { out: 950000, uncached: 4800000 })));
  const [state, detail] = verdict('claude-sonnet-5', stats['claude-sonnet-5'], SONNET);
  assert.equal(state, 'both-limiters-saturated');
  assert.match(detail, /does nothing for the output side/);
});

test('the input recorded is from the minute output peaked', () => {
  const buckets = [...Array(20).keys()].map((i) =>
    minute(stamp(i), 'claude-opus-5', { out: 200000, uncached: 400000 }));
  buckets[5] = minute(stamp(5), 'claude-opus-5', { out: 980000, uncached: 1200000 });
  buckets[12] = minute(stamp(12), 'claude-opus-5', { out: 300000, uncached: 4900000 });
  const row = peaks(buckets)['claude-opus-5'];
  assert.equal(row.peak_out, 980000);
  assert.equal(row.peak_at, '2026-08-30T14:05:00Z');
  assert.equal(row.input_at_peak, 1200000);
  assert.equal(verdict('claude-opus-5', row, SONNET)[0], 'otpm-saturated');
});

test('input is summed from every field that carries it', () => {
  const result = { output_tokens: 50, uncached_input_tokens: 100,
    cache_read_input_tokens: 900,
    cache_creation: { ephemeral_5m_input_tokens: 7, ephemeral_1h_input_tokens: 3 } };
  assert.equal(generated(result), 50);
  assert.equal(received(result), 1010);
  assert.equal(generated({}), 0);
  assert.equal(generated(null), 0);
  assert.equal(received(null), 0);
});

test('the implied answer length refuses to guess', () => {
  assert.equal(impliedMeanOutput(980000, null), null);
  assert.equal(impliedMeanOutput(980000, 0), null);
  assert.equal(impliedMeanOutput(0, 4000), null);
  assert.equal(outputToInputRatio({ output_tokens_per_minute: 1000 }), null);
  assert.equal(outputToInputRatio(null), null);
});

test('an unpublished output ceiling gets no verdict', () => {
  const groups = limitsByGroup({ data: [
    { model_group: 'claude-sonnet-5', limits: [
      { type: 'requests_per_minute', value: 4000 },
      { type: 'input_tokens_per_minute', value: 5000000 },
      { type: 'output_tokens_per_minute', value: 1000000 }] },
    { model_group: 'claude-fable-5', limits: [
      { type: 'requests_per_minute', value: 500 }] },
  ] });
  assert.deepEqual(limitsFor(groups, 'claude-sonnet-5-20260101'), SONNET);
  const fable = limitsFor(groups, 'claude-fable-5');
  assert.equal(fable.output_tokens_per_minute, null);
  assert.equal(verdict('claude-fable-5', { minutes: 60, peak_out: 9 }, fable)[0],
               'no-limit-published');
  assert.equal(limitsFor(groups, 'claude-haiku-4-5-20251001'), null);
  assert.equal(verdict('claude-opus-5', { minutes: 2, peak_out: 9 }, SONNET)[0],
               'too-few-buckets');
});
