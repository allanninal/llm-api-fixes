import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, fold, isReasoningModel, modelVerdict, repairLines, silentShare }
  from './openai-zero-output-buckets.mjs';

const bucket = (project, model, made, input, output) => ({
  results: [{ project_id: project, model, num_model_requests: made,
              input_tokens: input, output_tokens: output }],
});

const rowFor = (buckets, project, model) =>
  [...fold(buckets).values()].find((r) => r.project === project && r.model === model);

test('requests with no tokens either side is a rejected body', () => {
  const buckets = Array.from({ length: 24 },
    () => bucket('proj_api', 'gpt-5.1', 500, 0, 0));
  const row = rowFor(buckets, 'proj_api', 'gpt-5.1');
  assert.equal(row.requests, 12000);
  assert.equal(row.buckets, 24);
  assert.equal(row.silentBuckets, 24);
  assert.equal(silentShare(row), 1);

  const [state, detail] = classify('gpt-5.1', row);
  assert.equal(state, 'parameter-rejected');
  assert.match(detail, /0 input token\(s\) and 0 output token\(s\)/);
  assert.match(repairLines('gpt-5.1')[0], /max_completion_tokens/);
  assert.match(repairLines('gpt-5.1')[1], /max_output_tokens/);
});

test('input read and nothing generated is a different finding', () => {
  const buckets = Array.from({ length: 24 },
    () => bucket('proj_api', 'gpt-5.1', 500, 900000, 0));
  const [state, detail] = classify('gpt-5.1', rowFor(buckets, 'proj_api', 'gpt-5.1'));
  assert.equal(state, 'generation-blocked');
  assert.match(detail, /verification/);
});

test('a partial rollout is not rounded up to a total outage', () => {
  const silent = Array.from({ length: 6 }, () => bucket('proj_api', 'o3-mini', 100, 0, 0));
  const healthy = Array.from({ length: 18 },
    () => bucket('proj_api', 'o3-mini', 100, 200000, 40000));
  const row = rowFor([...silent, ...healthy], 'proj_api', 'o3-mini');
  assert.equal(silentShare(row), 0.25);
  const [state, detail] = classify('o3-mini', row);
  assert.equal(state, 'partial-rejection');
  assert.match(detail, /25%/);
});

test('the reasoning families are matched as whole prefixes', () => {
  for (const model of ['o1', 'o3-mini', 'o4-mini', 'gpt-5', 'gpt-5.1-mini',
                       'gpt-5-2026-01-15']) {
    assert.equal(isReasoningModel(model), true, model);
  }
  for (const model of ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'claude-sonnet-5', '', null]) {
    assert.equal(isReasoningModel(model), false, String(model));
  }
  assert.match(repairLines('gpt-4o')[0], /reasoning families/);
});

test('a quiet row is not a silent one', () => {
  assert.equal(silentShare({ requests: 0, silentRequests: 0 }), null);
  assert.equal(silentShare(null), null);
  assert.equal(classify('gpt-5.1', { requests: 4, silentRequests: 4 })[0], 'too-few-requests');
  const healthy = rowFor([bucket('p', 'gpt-5.1', 500, 200000, 60000)], 'p', 'gpt-5.1');
  assert.equal(classify('gpt-5.1', healthy)[0], 'generating');
});

test('a 404 on the model lookup is a different note entirely', () => {
  assert.equal(modelVerdict(200)[0], 'id-resolves');
  assert.equal(modelVerdict(404)[0], 'id-unreachable');
  assert.match(modelVerdict(404)[1], /retirement or entitlement/);
  assert.equal(modelVerdict(403)[0], 'check-refused');
  assert.equal(modelVerdict(null)[0], 'unchecked');
});

test('unreadable usage fields do not become phantom requests', () => {
  const row = rowFor([{ results: [{ project_id: 'p', model: 'gpt-5.1',
                                    num_model_requests: null,
                                    input_tokens: 'nonsense',
                                    output_tokens: null }] }], 'p', 'gpt-5.1');
  assert.equal(row.requests, 0);
  assert.equal(row.silentBuckets, 0);
  assert.equal(fold([]).size, 0);
  assert.equal(fold(null).size, 0);
});
