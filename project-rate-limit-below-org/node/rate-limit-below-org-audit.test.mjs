import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gradeOverride, groupLabel, inheritedLimiters, limitsOf, num, openaiMatrix,
         openaiOutliers, orgIndex, overridesOf, repairLines, verdict }
  from './rate-limit-below-org-audit.mjs';

const orgGroup = (models, limits) => ({
  type: 'rate_limit', group_type: 'model_group', models: [...models],
  limits: Object.entries(limits ?? {}).map(([type, value]) => ({ type, value })),
});

const wsGroup = (models, limits) => ({
  type: 'workspace_rate_limit', group_type: 'model_group', models: [...models], limits,
});

test('a workspace capped at a fraction of the org is the finding', () => {
  const entry = wsGroup(['claude-opus-5'], [
    { type: 'input_tokens_per_minute', value: 500000, org_limit: 10000000 },
    { type: 'requests_per_minute', value: 1000, org_limit: 4000 },
  ]);
  const rows = overridesOf(entry);
  assert.deepEqual(rows.map((r) => r[0]),
                   ['requests_per_minute', 'input_tokens_per_minute']);
  const [state, detail] = gradeOverride(500000, 10000000);
  assert.equal(state, 'throttled-below-org');
  assert.equal(detail, '500,000 of 10,000,000 (5%)');
  assert.equal(verdict(rows.map((r) => gradeOverride(r[1], r[2])[0])),
               'throttled-below-org');
  assert.ok(repairLines(state).some((l) => l.includes('Rate limits tab')));
});

test('an override equal to the org value is a pin, not a no-op', () => {
  const [state, detail] = gradeOverride(10000000, 10000000);
  assert.equal(state, 'override-pinned-at-org');
  assert.match(detail, /will not follow the next increase/);
  assert.ok(repairLines(state).some((l) => l.includes('Delete the override')));
  const [above, aboveDetail] = gradeOverride(20000000, 10000000);
  assert.equal(above, 'override-above-org');
  assert.match(aboveDetail, /applies anyway/);
});

test('a null org_limit is unjudgeable and never becomes zero', () => {
  assert.equal(num(null), null);
  assert.equal(num('nope'), null);
  assert.equal(num(true), null);
  const [state, detail] = gradeOverride(500000, null);
  assert.equal(state, 'org-limit-unknown');
  assert.match(detail, /cannot be graded/);
  assert.equal(gradeOverride(0, 10000000)[0], 'throttled-below-org');
});

test('limiters absent from an overridden group are reported as inherited', () => {
  const org = orgIndex([{ data: [orgGroup(['claude-opus-5'], {
    requests_per_minute: 4000,
    input_tokens_per_minute: 10000000,
    output_tokens_per_minute: 2000000,
  })] }]);
  const label = groupLabel(orgGroup(['claude-opus-5'], {}));
  const entry = wsGroup(['claude-opus-5'], [
    { type: 'input_tokens_per_minute', value: 500000, org_limit: 10000000 },
    { type: 'requests_per_minute', value: 1000, org_limit: 4000 },
  ]);
  assert.deepEqual(inheritedLimiters(entry, org[label]),
                   [['output_tokens_per_minute', 2000000]]);
  assert.equal(verdict(['override-in-range', 'limiter-inherited']), 'limiter-inherited');
});

test('the two endpoints label the same group identically', () => {
  const models = ['claude-opus-4-8', 'claude-opus-4-5'];
  assert.equal(groupLabel(orgGroup(models, {})), groupLabel(wsGroup(models, [])));
  assert.equal(groupLabel(orgGroup(models, {})), 'model_group:claude-opus-4-5 +1');
  assert.equal(groupLabel({ group_type: 'batch', models: null }), 'batch');
  assert.equal(groupLabel(null), 'unknown_group');
  assert.deepEqual(limitsOf({ limits: [{ type: 'x', value: 'not-a-number' }] }), {});
});

test('openai needs a peer because the object has no org value', () => {
  const one = openaiMatrix({ proj_a: [{ model: 'gpt-5.6',
    max_requests_per_1_minute: 60, max_tokens_per_1_minute: 150000 }] });
  assert.deepEqual(openaiOutliers(one), []);
  const both = openaiMatrix({
    proj_a: [{ model: 'gpt-5.6', max_requests_per_1_minute: 10000,
               max_tokens_per_1_minute: 2000000 }],
    proj_b: [{ model: 'gpt-5.6', max_requests_per_1_minute: 9000,
               max_tokens_per_1_minute: 150000 },
             { model: '', max_tokens_per_1_minute: 1 }],
  });
  assert.deepEqual(Object.keys(both['gpt-5.6'] ?? {}).sort(), ['proj_a', 'proj_b']);
  assert.equal(both[''], undefined);
  assert.deepEqual(openaiOutliers(both),
                   [['gpt-5.6', 'proj_b', 'tpm', 150000, 2000000]]);
  assert.ok(repairLines('project-outlier').some((l) => l.includes('proxy for the tier')));
});

test('empty and absent inputs do not raise', () => {
  assert.deepEqual(orgIndex(null), {});
  assert.deepEqual(overridesOf(null), []);
  assert.deepEqual(openaiMatrix(null), {});
  assert.deepEqual(openaiOutliers(null), []);
  assert.deepEqual(inheritedLimiters(null, null), []);
  assert.equal(verdict([]), 'no-override');
  assert.equal(verdict(null), 'no-override');
  assert.equal(gradeOverride(null, 10)[0], 'no-override');
  assert.deepEqual(repairLines('no-override'), []);
});
