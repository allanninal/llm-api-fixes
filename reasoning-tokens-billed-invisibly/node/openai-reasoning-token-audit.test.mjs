import { test } from 'node:test';
import assert from 'node:assert/strict';
import { split, totals, verdict } from './openai-reasoning-token-audit.mjs';

const NOW = new Date('2026-08-30T00:00:00Z');
const daysAgo = (d) => Math.floor(NOW.getTime() / 1000 - d * 86400);

const day = (d, requests = 100, inp = 90000, out = 100000, model = 'gpt-5.6') => ({
  start_time: daysAgo(d),
  results: [{ model, num_model_requests: requests, input_tokens: inp, output_tokens: out }],
});

// No num_model_requests: that field does not exist on Anthropic's report.
const anthropicDay = (d, inp = 90000, out = 100000) => ({
  start_time: daysAgo(d),
  results: [{ uncached_input_tokens: inp, output_tokens: out }],
});

test('totals sums and tolerates a missing request count', () => {
  assert.deepEqual(totals([day(1), day(2)]),
    { requests: 200, input: 180000, output: 200000, buckets: 2 });
  assert.equal(totals([anthropicDay(1)]).requests, 0);
});

test('split cuts the series at the clock it is given', () => {
  const [prior, recent] = split([day(1), day(3), day(9), day(30)], NOW, 7);
  assert.deepEqual(recent.map((b) => b.start_time), [daysAgo(1), daysAgo(3)]);
  assert.deepEqual(prior.map((b) => b.start_time), [daysAgo(9)]);
});

test('the finding: output per request rises while input holds', () => {
  const [state, detail] = verdict([day(9, 100, 90000, 100000)],
    [day(1, 100, 91000, 400000)]);
  assert.equal(state, 'reasoning-tax');
  assert.match(detail, /4\.0x/);
  assert.match(detail, /never returned/);
});

test('prompts growing is not the same finding', () => {
  assert.equal(verdict([day(9, 100, 90000, 100000)],
    [day(1, 100, 360000, 400000)])[0], 'longer-prompts');
});

test('more traffic at the same ratios is not a finding at all', () => {
  const [state, detail] = verdict([day(9, 100, 90000, 100000)],
    [day(1, 400, 360000, 400000)]);
  assert.equal(state, 'volume-only');
  assert.match(detail, /unit economics/);
});

test('flat ratios and flat traffic are steady', () => {
  assert.equal(verdict([day(9, 100, 90000, 100000)],
    [day(1, 110, 99000, 110000)])[0], 'steady');
});

test('no request count degrades to a weaker claim and says so', () => {
  const [state, detail] = verdict([anthropicDay(9, 90000, 100000)],
    [anthropicDay(1, 90000, 400000)]);
  assert.equal(state, 'unmeasurable-but-rising');
  assert.match(detail, /per input token, not per request/);
  assert.equal(verdict([anthropicDay(9)], [anthropicDay(1)])[0], 'unmeasurable');
});

test('requests with no output is an error shape, not a reasoning one', () => {
  assert.equal(verdict([day(9)], [day(1, 50, 45000, 0)])[0],
    'failing-before-generation');
});

test('an empty recent window claims nothing', () => {
  assert.equal(verdict([day(9)], [])[0], 'no-data');
});
