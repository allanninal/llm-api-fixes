import { test } from 'node:test';
import assert from 'node:assert/strict';
import { audit, gradeBetas, gradeCeiling, gradePremium, parseRules,
         repairLines, reportedOutput, reportedWindow, shortfall, validModelId }
  from './anthropic-context-window-cap.mjs';

const OPUS_5 = { id: 'claude-opus-5', max_input_tokens: 1_000_000,
                 max_output_tokens: 128_000 };
const SONNET_4_5 = { id: 'claude-sonnet-4-5', max_input_tokens: 200_000,
                     max_output_tokens: 64_000 };
const HAIKU = { id: 'claude-haiku-4-5-20251001', max_input_tokens: 200_000,
                max_output_tokens: 64_000 };

test('a million token window enforced at two hundred thousand', () => {
  const rules = parseRules({ 'claude-opus-5': { max_input_tokens: 200_000 } });
  const [state, detail] = gradeCeiling(reportedWindow(OPUS_5),
                                       rules['claude-opus-5'].cap);
  assert.equal(state, 'capped-in-code');
  assert.match(detail, /800000 token\(s\) of window bought and unreachable/);
  assert.equal(shortfall(1_000_000, 200_000), 800_000);
  assert.ok(repairLines(state, 'claude-opus-5')
    .some((line) => line.includes('raise the enforced ceiling')));
});

test('the opposite direction is a different and louder fault', () => {
  const [state, detail] = gradeCeiling(reportedWindow(SONNET_4_5), 1_000_000);
  assert.equal(state, 'cap-above-model');
  assert.match(detail, /400 prompt is too long/);
  assert.equal(gradeCeiling(reportedWindow(HAIKU), 200_000)[0], 'aligned');
});

test('the same beta header is two findings depending on the model', () => {
  const inert = gradeBetas(reportedWindow(OPUS_5), ['context-1m-2025-08-07']);
  assert.deepEqual(inert.map(([s]) => s), ['inert-beta-header']);
  assert.match(inert[0][1], /does nothing/);

  const retired = gradeBetas(reportedWindow(SONNET_4_5), ['context-1m-2025-08-07']);
  assert.deepEqual(retired.map(([s]) => s), ['retired-beta']);
  assert.match(retired[0][1], /2026-04-30/);
  assert.deepEqual(gradeBetas(1_000_000, ['some-other-beta']), []);
  assert.deepEqual(gradeBetas(null, ['context-1m-2025-08-07']), []);
});

test('a long context premium branch prices something that is free', () => {
  const [state, detail] = gradePremium(reportedWindow(OPUS_5), true);
  assert.equal(state, 'phantom-premium');
  assert.match(detail, /same per-token rate/);
  assert.equal(gradePremium(reportedWindow(OPUS_5), false), null);
  assert.equal(gradePremium(reportedWindow(SONNET_4_5), true), null);
});

test('one stale id carries several findings at once', () => {
  const rules = parseRules({ 'claude-opus-5': {
    max_input_tokens: 200_000,
    beta_headers: 'context-1m-2025-08-07',
    long_context_premium: true } });
  assert.deepEqual(audit(OPUS_5, rules['claude-opus-5']).map(([s]) => s),
    ['capped-in-code', 'inert-beta-header', 'phantom-premium']);
  const clean = parseRules({ 'claude-haiku-4-5-20251001': { max_input_tokens: 200_000 } });
  assert.deepEqual(audit(HAIKU, clean['claude-haiku-4-5-20251001']).map(([s]) => s),
    ['aligned']);
});

test('model ids are validated before they reach a url', () => {
  assert.equal(validModelId('claude-opus-5'), true);
  assert.equal(validModelId('claude-haiku-4-5-20251001'), true);
  assert.equal(validModelId('../../organizations'), false);
  assert.equal(validModelId('claude opus 5'), false);
  assert.equal(validModelId(''), false);
  assert.equal(validModelId(null), false);
  const rules = parseRules({ '../../etc': { max_input_tokens: 1 },
                             'claude-opus-5': { max_input_tokens: 200_000 } });
  assert.deepEqual(Object.keys(rules), ['claude-opus-5']);
});

test('a missing window is not a window of zero', () => {
  assert.equal(reportedWindow({}), null);
  assert.equal(reportedWindow({ max_input_tokens: 0 }), null);
  assert.equal(reportedWindow({ max_input_tokens: '1000000' }), 1_000_000);
  assert.equal(reportedOutput(OPUS_5), 128_000);
  assert.equal(shortfall(null, 200_000), null);
  const [state, detail] = gradeCeiling(null, 200_000);
  assert.equal(state, 'window-not-reported');
  assert.match(detail, /no claim is made/);
});

test('rules default safely when the config is thin', () => {
  const rules = parseRules({ 'claude-opus-5': {} });
  assert.deepEqual(rules['claude-opus-5'], { cap: null, betas: [], premium: false });
  assert.deepEqual(audit(OPUS_5, rules['claude-opus-5']), []);
  assert.deepEqual(parseRules(null), {});
  assert.equal(parseRules({ 'claude-opus-5': 'not a dict' })['claude-opus-5'].cap,
               null);
});
