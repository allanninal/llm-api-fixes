import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fold, permissionsState, sibling, spendFor, tier, verdict }
  from './openai-model-rightsizing-audit.mjs';

/** A folded row shaped like fold() returns them. */
function row({ requests = 10000, output = 190000, input = 900000,
               projects = ['proj_a'] } = {}) {
  return { requests, output, input, projects };
}

/** One daily bucket from GET /v1/organization/usage/completions. */
function bucket(results) {
  return {
    data: [{
      start_time: 0,
      results: Object.entries(results).map(([model, [r, i, o, p]]) => ({
        model, num_model_requests: r, input_tokens: i, output_tokens: o,
        project_id: p,
      })),
    }],
  };
}

test('a premium model with tiny answers is the finding', () => {
  const [state, detail] = verdict('gpt-5',
    row({ requests: 412880, output: 7844720, input: 170000000 }));
  assert.equal(state, 'oversized');
  assert.match(detail, /mean output 19 token/);
  assert.equal(sibling('gpt-5'), 'gpt-5-mini');
});

test('the same shape on the mini sibling is not a finding', () => {
  const [state] = verdict('gpt-5-mini',
    row({ requests: 412880, output: 7844720, input: 170000000 }));
  assert.equal(state, 'right-sized');
});

test('long answers are the model doing its job', () => {
  const [state, detail] = verdict('gpt-5',
    row({ requests: 9000, output: 18000000, input: 9000000 }));
  assert.equal(state, 'deliberative');
  assert.match(detail, /mean output 2000 token/);
});

test('short answers over huge prompts are a caching problem', () => {
  const [state, detail] = verdict('gpt-4.1',
    row({ requests: 5000, output: 95000, input: 200000000 }));
  assert.equal(state, 'input-bound');
  assert.match(detail, /caching the prefix/);
});

test('a model too quiet to have a shape gets no verdict', () => {
  assert.equal(verdict('gpt-5', row({ requests: 40, output: 760 }))[0], 'low-volume');
  assert.equal(verdict('gpt-5', row({ requests: 0, output: 0 }))[0], 'unreadable');
});

test('tiers are conservative about what they claim to know', () => {
  assert.equal(tier('ft:gpt-4o-mini-2024-07-18:acme::AbC123'), 'custom');
  assert.equal(tier('text-embedding-3-large'), 'small');
  assert.equal(tier('some-model-we-have-never-heard-of'), 'unknown');
  assert.equal(sibling('some-model-we-have-never-heard-of'), null);
  assert.equal(verdict('ft:gpt-4o-2024-08-06:acme::X', row())[0], 'custom-model');
  assert.equal(verdict('some-model-we-have-never-heard-of', row())[0], 'unknown-model');
});

test('buckets are folded before the division', () => {
  const pages = [bucket({ 'gpt-5': [100, 50000, 1000, 'proj_a'] }),
                 bucket({ 'gpt-5': [900, 450000, 9000, 'proj_b'] })];
  const folded = fold(pages);
  assert.equal(folded['gpt-5'].requests, 1000);
  assert.equal(folded['gpt-5'].output, 10000);
  assert.deepEqual(folded['gpt-5'].projects, ['proj_a', 'proj_b']);
  assert.match(verdict('gpt-5', folded['gpt-5'], 100)[1], /mean output 10 token/);
});

test('permissions say whether the expensive model can come back', () => {
  assert.equal(permissionsState({ mode: 'deny_list', model_ids: [] }, 'gpt-5'),
               'unconstrained');
  assert.equal(permissionsState({ mode: 'deny_list', model_ids: ['gpt-5'] }, 'gpt-5'),
               'blocked');
  assert.equal(permissionsState({ mode: 'allow_list', model_ids: ['gpt-5-mini'] }, 'gpt-5'),
               'blocked');
  assert.equal(permissionsState({ mode: 'allow_list', model_ids: ['gpt-5'] }, 'gpt-5'),
               'allowed');
  assert.equal(permissionsState({}, 'gpt-5'), 'unreadable');
  assert.equal(permissionsState(null, 'gpt-5'), 'unreadable');
});

test('spend is matched to the model and not to its siblings', () => {
  const spend = {
    'gpt-5, input tokens': 3000.00,
    'gpt-5, output tokens': 411.20,
    'gpt-5-mini, input tokens': 90.00,
    'ft:gpt-5:acme::x, input tokens': 12.00,
  };
  assert.equal(spendFor('gpt-5', spend), 3411.20);
  assert.equal(spendFor('gpt-5-mini', spend), 90.00);
  assert.equal(spendFor('', spend), 0);
  assert.equal(spendFor('gpt-5', {}), 0);
});
