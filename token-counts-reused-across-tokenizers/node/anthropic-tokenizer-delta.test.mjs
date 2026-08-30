import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TOLERANCE, countBody, parseBudgets, ratio, rebaseline, repairLines,
         sameApartFromModel, swapModel, verdict,
         workloadRatio } from './anthropic-tokenizer-delta.mjs';

const BODY = {
  model: 'claude-sonnet-4-6',
  system: 'You are a scientist',
  messages: [{ role: 'user', content: 'Hello, Claude' }],
  tools: [{ name: 'get_weather', description: 'weather',
            input_schema: { type: 'object', properties: {} } }],
  thinking: { type: 'enabled', budget_tokens: 16000 },
  max_tokens: 1024,
  temperature: 0.2,
};

test('a body that drifted never produces a ratio', () => {
  const left = swapModel(countBody(BODY), 'claude-sonnet-4-6');
  const right = swapModel(countBody(BODY), 'claude-opus-5');
  assert.ok(sameApartFromModel(left, right));
  assert.ok(!sameApartFromModel(left, { ...right, system: 'You are a careful scientist' }));
  const [state, detail] = verdict([{ name: 'a.json', mismatch: true }],
                                  'claude-sonnet-4-6', 'claude-opus-5');
  assert.equal(state, 'bodies-differ');
  assert.ok(detail.includes('no ratio was taken'));
  assert.ok(repairLines(state, null).some((l) => l.includes('swap only model')));
});

test('the workload ratio is token weighted and not a mean of ratios', () => {
  const rows = [{ baseTokens: 40000, targetTokens: 52000, ratio: 1.3 },
                { baseTokens: 200, targetTokens: 400, ratio: 2.0 }];
  assert.ok(Math.abs(workloadRatio(rows) - (52400 / 40200)) < 1e-9);
  assert.ok(workloadRatio(rows) < 1.32);
  assert.equal(workloadRatio([]), null);
  assert.equal(workloadRatio([{ baseTokens: 0, targetTokens: 10 }]), null);
});

test('two ids on the same tokenizer are a non finding', () => {
  const rows = [{ name: 'a.json', baseTokens: 1000, targetTokens: 1005, ratio: 1.005 }];
  const [state, detail] = verdict(rows, 'claude-opus-5', 'claude-sonnet-5');
  assert.equal(state, 'counts-agree');
  assert.ok(detail.includes('share a tokenizer'));
  assert.ok(repairLines(state, 1.005).some((l) => l.includes('transfer to the other')));
  assert.ok(Math.abs(1.005 - 1) < TOLERANCE);
});

test('the delta is reported with what it costs and what it breaks', () => {
  const rows = [{ name: 'a.json', baseTokens: 18204, targetTokens: 23551, ratio: 1.2937 }];
  const [state, detail] = verdict(rows, 'claude-sonnet-4-6', 'claude-opus-5');
  assert.equal(state, 'tokenizer-delta');
  assert.ok(detail.includes('claude-opus-5') && detail.includes('1.294'));
  const lines = repairLines(state, workloadRatio(rows));
  assert.ok(lines.some((l) => l.includes('key any stored token count by model')));
  assert.ok(lines.some((l) => l.includes('29%')));
  assert.ok(lines.some((l) => l.includes('retrieval quality')));
});

test('counting bodies drop generation fields and keep the window', () => {
  const counted = countBody(BODY);
  assert.ok(!('max_tokens' in counted) && !('temperature' in counted));
  for (const kept of ['system', 'messages', 'tools', 'thinking']) {
    assert.ok(kept in counted);
  }
  assert.deepEqual(countBody(null), {});
  assert.equal(swapModel(counted, 'claude-fable-5').model, 'claude-fable-5');
  assert.equal(counted.model, 'claude-sonnet-4-6');
});

test('budgets are parsed forgivingly and rebaselined in order', () => {
  const budgets = parseBudgets(['history=120000,chunk=800', 'junk', 'bad=x', 'zero=0']);
  assert.deepEqual(budgets, { history: 120000, chunk: 800 });
  assert.deepEqual(rebaseline(budgets, 1.33),
                   [['chunk', 800, 1064], ['history', 120000, 159600]]);
  assert.deepEqual(rebaseline(budgets, null), []);
});

test('a 413 is handed to the byte note rather than counted', () => {
  const rows = [{ name: 'big.json',
                  error: 'HTTP 413 Request exceeds the maximum allowed number of bytes.' }];
  const [state, detail] = verdict(rows, 'claude-sonnet-4-6', 'claude-opus-5');
  assert.equal(state, 'count-failed');
  assert.ok(detail.includes('413'));
  assert.ok(repairLines(state, null).some((l) => l.includes('32 MB byte ceiling')));
  assert.equal(ratio(0, 10), null);
  assert.equal(ratio(null, 10), null);
  assert.equal(verdict([], 'a', 'b')[0], 'no-bodies');
});
