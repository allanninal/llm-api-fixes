import { test } from 'node:test';
import assert from 'node:assert/strict';
import { baseModel, daysUntil, verdict }
  from './openai-fine-tune-usage-audit.mjs';

const NOW = new Date(Date.UTC(2026, 7, 30, 12, 0, 0));
const LIVE = ['gpt-4o-mini-2024-07-18', 'gpt-5', 'gpt-5-mini'];

function job({ status = 'succeeded',
               modelId = 'ft:gpt-4o-mini-2024-07-18:acme::AbC123',
               base = 'gpt-4o-mini-2024-07-18', trained = 4182900,
               ...extra } = {}) {
  return {
    id: 'ftjob-test', status, fine_tuned_model: modelId, model: base,
    trained_tokens: trained, ...extra,
  };
}

test('trained, billed and never called', () => {
  const [state, detail] = verdict(job(), 0, LIVE, NOW);
  assert.equal(state, 'never-called');
  assert.match(detail, /0 request.s. in 30 days/);
  assert.match(detail, /4182900 trained token/);
});

test('a model serving traffic is not a finding', () => {
  assert.equal(verdict(job(), 91204, LIVE, NOW)[0], 'in-service');
});

test('a vanished base model changes both answers', () => {
  const idle = verdict(job({ base: 'gpt-4-0613', modelId: 'ft:gpt-4-0613:acme::Old1' }),
                       0, LIVE, NOW);
  assert.equal(idle[0], 'never-called-base-gone');
  assert.match(idle[1], /no longer listed/);
  assert.match(idle[1], /stop answering in 53 day/);

  const live = verdict(job({ base: 'gpt-4-0613', modelId: 'ft:gpt-4-0613:acme::Old1' }),
                       50000, LIVE, NOW);
  assert.equal(live[0], 'in-service-base-gone');
  assert.match(live[1], /going to stop/);
});

test('jobs that produced nothing are not this note', () => {
  for (const status of ['failed', 'running', 'cancelled']) {
    assert.equal(verdict(job({ status }), 0, LIVE, NOW)[0], 'not-succeeded');
  }
  const [state, detail] = verdict(job({ modelId: null }), 0, LIVE, NOW);
  assert.equal(state, 'unnamed');
  assert.match(detail, /by hand/);
});

test('the base is the second field not the last one', () => {
  assert.equal(baseModel('ft:gpt-4o-mini-2024-07-18:acme::AbC123'),
               'gpt-4o-mini-2024-07-18');
  assert.equal(baseModel('ft:gpt-4o-2024-08-06:acme:nightly:AbC123'),
               'gpt-4o-2024-08-06');
  assert.equal(baseModel('gpt-5'), null);
  assert.equal(baseModel(''), null);
  assert.equal(baseModel(null), null);
});

test('the deadline is floored toward the past', () => {
  assert.equal(daysUntil('2026-10-23', NOW), 53);
  assert.equal(daysUntil('2026-08-31', NOW), 0);
  assert.equal(daysUntil('2026-08-30', NOW), -1);
  assert.equal(daysUntil('not-a-date', NOW), null);
});

test('a job with no base field falls back to the model id', () => {
  const [state] = verdict({
    id: 'ftjob-x', status: 'succeeded',
    fine_tuned_model: 'ft:gpt-4-0613:acme::Old1', trained_tokens: 100,
  }, 0, LIVE, NOW);
  assert.equal(state, 'never-called-base-gone');
});
