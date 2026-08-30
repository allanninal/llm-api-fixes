import { test } from 'node:test';
import assert from 'node:assert/strict';
import { burstiness, classify, divergenceRatio, foldWindows, growth,
         limiterPressure, rateLimitValues, series, tokensPerRequest }
  from './openai-retry-storm-shape.mjs';

const CUTOFF = 1000000;
const HOUR = 3600;

const hours = (start, count, requests, tokens) =>
  Array.from({ length: count }, (_, i) => ({ start: start + i * HOUR, requests, tokens }));

const PRIOR_WEEK = hours(CUTOFF - 168 * HOUR, 168, 1000, 5000000);
// Same weekly totals, two different shapes.
const STORM = [...hours(CUTOFF, 150, 1000, 5000000),
               ...hours(CUTOFF + 150 * HOUR, 18, 20000, 5000000)];
const EVEN = hours(CUTOFF, 168, 3000, 5000000);

test('requests climb in bursts while tokens stand still', () => {
  const [prior, recent] = foldWindows([...PRIOR_WEEK, ...STORM], CUTOFF);
  assert.equal(prior.requests, 168000);
  assert.equal(prior.tokens, 840000000);
  assert.equal(recent.requests, 510000);
  assert.equal(recent.tokens, 840000000);
  assert.equal(Number(growth(prior.requests, recent.requests).toFixed(3)), 3.036);
  assert.equal(growth(prior.tokens, recent.tokens), 1);
  assert.equal(Math.trunc(tokensPerRequest(prior)), 5000);
  assert.equal(Math.trunc(tokensPerRequest(recent)), 1647);

  const burst = burstiness([...PRIOR_WEEK, ...STORM], CUTOFF);
  assert.equal(Number(burst.toFixed(3)), 0.667);
  const [state, detail] = classify(prior, recent, burst);
  assert.equal(state, 'retry-storm');
  assert.match(detail, /requests x3\.04, tokens x1\.00/);
  assert.match(detail, /tokens per request 5000 then 1647/);
  assert.match(detail, /67% of the surplus landed in the busiest 10% of hours/);
});

test('the same ratios spread evenly are not a storm', () => {
  const [prior, recent] = foldWindows([...PRIOR_WEEK, ...EVEN], CUTOFF);
  assert.equal(Number(divergenceRatio(prior, recent).toFixed(2)), 3);
  const burst = burstiness([...PRIOR_WEEK, ...EVEN], CUTOFF);
  assert.equal(Number(burst.toFixed(3)), 0.101);
  const [state, detail] = classify(prior, recent, burst);
  assert.equal(state, 'requests-outpacing-tokens');
  assert.match(detail, /spread evenly across the hours/);
});

test('the divergence ratio is the mean call size inverted', () => {
  const [prior, recent] = foldWindows([...PRIOR_WEEK, ...STORM], CUTOFF);
  const identity = tokensPerRequest(prior) / tokensPerRequest(recent);
  assert.equal(divergenceRatio(prior, recent).toFixed(9), identity.toFixed(9));
});

test('a real customer moves both series together', () => {
  const [state, detail] = classify({ requests: 100000, tokens: 500000000 },
                                   { requests: 300000, tokens: 1500000000 }, 0.1);
  assert.equal(state, 'traffic-growth');
  assert.match(detail, /moved together/);
});

test('a prompt that grew moves only the token series', () => {
  const [state] = classify({ requests: 100000, tokens: 200000000 },
                           { requests: 100000, tokens: 600000000 }, 0.1);
  assert.equal(state, 'prompts-grew');
});

test('the partial hour is dropped before anything is divided', () => {
  const tail = [{ start: CUTOFF + 200 * HOUR, requests: 1, tokens: 10 }];
  const [, recent] = foldWindows([...PRIOR_WEEK, ...EVEN, ...tail], CUTOFF,
                                 CUTOFF + 200 * HOUR);
  assert.equal(recent.buckets, 168);
  assert.equal(recent.requests, 504000);
  assert.equal(burstiness([...EVEN, ...tail], CUTOFF, CUTOFF + 200 * HOUR),
               burstiness(EVEN, CUTOFF));
});

test('a workload with no prior week has no growth rate', () => {
  assert.equal(growth(0, 5000), null);
  assert.equal(growth(null, 5000), null);
  assert.equal(divergenceRatio({ requests: 1, tokens: 0 },
                               { requests: 2, tokens: 0 }), null);
  assert.equal(tokensPerRequest({ requests: 0, tokens: 0 }), null);
  assert.equal(classify({ requests: 0, tokens: 0 },
                        { requests: 40000, tokens: 90000000 })[0], 'new-workload');
  assert.equal(classify({ requests: 10, tokens: 900 },
                        { requests: 12, tokens: 1000 })[0], 'too-little-traffic');
});

test('too few hours reports no concentration rather than a wrong one', () => {
  const short = Array.from({ length: 6 },
    (_, i) => ({ start: CUTOFF + i * HOUR, requests: 10, tokens: 1 }));
  assert.equal(burstiness(short, CUTOFF), null);
  assert.equal(burstiness([], CUTOFF), null);
  const [state, detail] = classify({ requests: 100000, tokens: 500000000 },
                                   { requests: 400000, tokens: 520000000 });
  assert.equal(state, 'retry-storm');
  assert.match(detail, /Too few hourly buckets/);
});

test('the request bucket is full while the token bucket is empty', () => {
  const payload = { data: [
    { model: 'gpt-5.1', max_requests_per_1_minute: 10000,
      max_tokens_per_1_minute: 20000000 },
    { model: 'gpt-5', max_requests_per_1_minute: 1, max_tokens_per_1_minute: 1 },
  ] };
  const limits = rateLimitValues(payload, 'gpt-5.1-2026-01-15');
  assert.deepEqual(limits, { requests: 10000, tokens: 20000000 });

  const [state, detail] = limiterPressure(
    { requests: 82656000, tokens: 18144000000 }, 168, limits);
  assert.equal(state, 'rpm-bound-tpm-idle');
  assert.match(detail, /82% of the RPM ceiling and 9% of the TPM ceiling/);
});

test('an unpublished limit is not a missing one', () => {
  assert.deepEqual(rateLimitValues({ data: [] }, 'gpt-5.1'),
                   { requests: null, tokens: null });
  assert.equal(limiterPressure({ requests: 1 }, 24, null)[0], 'no-limits-published');
  assert.equal(series([]).size, 0);
  assert.equal(series(null).size, 0);
});
