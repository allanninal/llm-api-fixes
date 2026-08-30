import { test } from 'node:test';
import assert from 'node:assert/strict';
import { duration, generationSeconds, prefillSeconds, safeMaxTokens,
         timeoutSeconds, unitSuspicion, verdict }
  from './anthropic-wall-clock-preflight.mjs';

test('the transport decides it and the prompt does not', () => {
  const seconds = prefillSeconds(2000) + generationSeconds(64000);
  assert.equal(duration(seconds), '19m 23s');

  const [state, detail] = verdict(seconds, false);
  assert.equal(state, 'over-wall-clock-not-streaming');
  assert.match(detail, /504/);
  assert.match(detail, /Raising the client timeout does not move it/);

  const [streamState, streamDetail] = verdict(seconds, true);
  assert.equal(streamState, 'streams-past-ten-minutes');
  assert.match(streamDetail, /never goes idle/);
});

test('an enormous prompt with a small answer is quick', () => {
  const seconds = prefillSeconds(60000) + generationSeconds(1024);
  assert.equal(duration(seconds), '0m 28s');
  assert.equal(verdict(seconds, false)[0], 'within-budget');
});

test('the models own cap is forty minutes of generation', () => {
  assert.equal(duration(generationSeconds(128000)), '38m 47s');
  assert.equal(safeMaxTokens(), 33000);
  assert.equal(safeMaxTokens(55, 600, 100), 27500);
  assert.equal(safeMaxTokens(0), 0);
});

test('six hundred means two different things in two sdks', () => {
  assert.equal(timeoutSeconds('python', 600), 600);
  assert.equal(timeoutSeconds('ruby', 600), 600);
  assert.equal(timeoutSeconds('typescript', 600), 0.6);
  assert.equal(timeoutSeconds('TypeScript', 600), 0.6);
  assert.equal(unitSuspicion('typescript', 600), true);
  assert.equal(unitSuspicion('node', 600), true);
  assert.equal(unitSuspicion('python', 600), false);
  assert.equal(unitSuspicion('typescript', 600000), false);
  assert.equal(timeoutSeconds('rust', 600), null);
  assert.equal(unitSuspicion('rust', 600), false);
  assert.equal(timeoutSeconds('python', null), null);
});

test('the wall clock is reported ahead of the client timeout', () => {
  assert.equal(verdict(1200, false, 300)[0], 'over-wall-clock-not-streaming');
  const [state, detail] = verdict(1200, true, 300);
  assert.equal(state, 'over-client-timeout');
  assert.match(detail, /gives up before the API is finished/);
});

test('a path close to the ceiling is reported before it crosses', () => {
  const [state, detail] = verdict(540, false);
  assert.equal(state, 'near-wall-clock-not-streaming');
  assert.match(detail, /inside 80% of the 10m 00s ceiling/);
  assert.equal(verdict(400, false)[0], 'within-budget');
});

test('durations read as minutes and seconds', () => {
  assert.equal(duration(0), '0m 00s');
  assert.equal(duration(59.9), '0m 59s');
  assert.equal(duration(600), '10m 00s');
  assert.equal(duration(-5), '0m 00s');
  assert.equal(duration(null), '0m 00s');
});
