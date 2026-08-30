import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, foldModels, policyIds, policyState, repairLines, unrestricted,
         unusedAllowed, unusedTools } from './openai-project-model-policy-audit.mjs';

const USED = { 'gpt-4.1-mini': 41208 };

const bucket = (...results) =>
  ({ object: 'bucket', start_time: 0, end_time: 86400, results });

test('an absent policy and an empty deny list are two findings', () => {
  const [absentState, absentDetail] = classify(null, USED);
  const [emptyState, emptyDetail] = classify({ mode: 'deny_list', model_ids: [] }, USED);

  assert.equal(unrestricted(null), true);
  assert.equal(unrestricted({ mode: 'deny_list', model_ids: [] }), true);
  assert.equal(absentState, 'no-policy');
  assert.equal(emptyState, 'deny-list-empty');
  assert.ok(emptyDetail.includes('looks configured'));
  assert.ok(absentDetail.includes('reachable from this project'));

  const absentLines = repairLines(absentState, 'proj_demo', USED);
  const emptyLines = repairLines(emptyState, 'proj_batch', USED);
  assert.ok(absentLines.some((l) => l.includes('does not inherit')));
  assert.ok(emptyLines.some((l) => l.includes('did not finish it')));
  assert.notDeepEqual(absentLines, emptyLines);
});

test('a deny list with entries is restrictive today and open tomorrow', () => {
  const policy = { mode: 'deny_list', model_ids: ['gpt-4.1'] };
  assert.equal(unrestricted(policy), false);
  const [state, detail] = classify(policy, USED);
  assert.equal(state, 'deny-list-fails-open');
  assert.ok(detail.includes('released tomorrow'));
  assert.ok(repairLines(state, 'proj_x', USED)
    .some((l) => l.includes('does not exist yet')));
  assert.deepEqual(unusedAllowed(policy, USED), []);
});

test('an allow list wider than use names only what it measured', () => {
  const policy = { mode: 'allow_list',
                   model_ids: ['gpt-4.1-mini', 'gpt-4.1', 'o3', 'gpt-4.1-nano'] };
  const [state, detail] = classify(policy, USED, 30);
  assert.equal(state, 'allow-list-wider-than-use');
  assert.ok(detail.includes('names 4 model(s); 1 served any request'));
  assert.deepEqual(unusedAllowed(policy, USED), ['gpt-4.1', 'gpt-4.1-nano', 'o3']);
  const lines = repairLines(state, 'proj_web', USED);
  assert.ok(lines.some((l) => l.includes('["gpt-4.1-mini"]')));
  assert.ok(!lines.some((l) => l.includes('o3')));
  const tight = { mode: 'allow_list', model_ids: ['gpt-4.1-mini'] };
  assert.equal(classify(tight, USED)[0], 'restricted');
  assert.deepEqual(repairLines('restricted', 'proj_web', USED), []);
});

test('the policy shape reader handles every degenerate case', () => {
  assert.equal(policyState(null), 'absent');
  assert.equal(policyState(undefined), 'absent');
  assert.equal(policyState({ mode: 'allow_list', model_ids: [] }), 'allow-empty');
  assert.equal(policyState({ mode: 'allow_list', model_ids: ['  '] }), 'allow-empty');
  assert.equal(policyState({ mode: 'ALLOW_LIST', model_ids: ['a'] }), 'allow-list');
  assert.equal(policyState({ mode: 'something_new' }), 'unreadable');
  assert.deepEqual(policyIds({ model_ids: ['a', '', null, ' b '] }), ['a', 'b']);
  const [state, detail] = classify({ mode: 'allow_list', model_ids: [] }, USED);
  assert.equal(state, 'allow-list-empty');
  assert.ok(detail.includes('permits nothing'));
  assert.equal(classify({ mode: '?' }, USED)[0], 'policy-unreadable');
});

test('a tool with no usage endpoint is uncountable, not unused', () => {
  const perms = { code_interpreter: { enabled: false },
                  file_search: { enabled: true },
                  image_generation: { enabled: true },
                  mcp: { enabled: true },
                  web_search: { enabled: true } };
  const counts = { web_search: 4120, file_search: 0, image_generation: 0 };
  const found = Object.fromEntries(unusedTools(perms, counts));
  assert.equal(found.web_search, undefined);
  assert.equal(found.code_interpreter, undefined);
  assert.ok(found.file_search.includes('file_search_calls reports nothing'));
  assert.equal(found.mcp, 'enabled, and no usage endpoint counts it');
  assert.deepEqual(unusedTools(null, null), []);
  assert.deepEqual(unusedTools({ web_search: 'not a block' }, {}), []);
});

test('the report never recommends a model the project did not call', () => {
  for (const state of ['no-policy', 'deny-list-empty', 'deny-list-fails-open',
                       'allow-list-wider-than-use']) {
    for (const line of repairLines(state, 'proj_a', USED)) {
      assert.ok(!line.includes('cheaper'));
    }
  }
  assert.ok(repairLines('no-policy', 'proj_idle', {})
    .some((l) => l.includes('no observed set')));
  const used = foldModels([
    bucket({ project_id: 'p', model: 'm', num_model_requests: 4 }),
    bucket({ project_id: 'p', model: 'm', num_model_requests: 0 })]);
  assert.deepEqual(used, { p: { m: 4 } });
});
