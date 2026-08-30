import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, coverage, fold, isRetired, repairLines, retiredIds }
  from './openai-moderation-coverage-audit.mjs';

const COMPLETION = 'organization.usage.completions.result';
const MODERATION = 'organization.usage.moderations.result';

const bucket = (...results) =>
  ({ object: 'bucket', start_time: 0, end_time: 86400, results });

const row = (project, model, n, obj = MODERATION) =>
  ({ object: obj, project_id: project, model, num_model_requests: n,
     input_tokens: n * 12 });

test('a busy project with no moderations entry survives the join', () => {
  const completions = fold([
    bucket(row('proj_public', 'gpt-4.1-mini', 20604, COMPLETION),
           row('proj_intake', 'gpt-4.1', 2000, COMPLETION)),
    bucket(row('proj_public', 'gpt-4.1-mini', 20604, COMPLETION))]);
  const moderations = fold([bucket(row('proj_intake', 'omni-moderation-latest', 1900))]);

  const rows = coverage(completions, moderations);
  assert.deepEqual(rows.map((r) => r[0]), ['proj_public', 'proj_intake']);

  const [state, detail] = classify(rows[0]);
  assert.equal(state, 'never-called');
  assert.ok(detail.includes('41208 completion request(s)'));
  const lines = repairLines(state, rows[0]);
  assert.ok(lines.some((l) => l.includes('bills nothing')));
  assert.ok(lines.some((l) => l.includes('category_scores')));

  assert.equal(classify(rows[1])[0], 'covered');
});

test('full coverage on a retired id is a finding, not coverage', () => {
  const completions = fold([bucket(row('proj_old', 'gpt-4.1', 4000, COMPLETION))]);
  const moderations = fold([bucket(row('proj_old', 'text-moderation-latest', 3904))]);
  const rows = coverage(completions, moderations);

  const [state, detail] = classify(rows[0]);
  assert.equal(state, 'retired-model-id');
  assert.ok(detail.includes('100% of them on text-moderation-latest'));
  const lines = repairLines(state, rows[0]);
  assert.ok(lines.some((l) => l.includes('omni-moderation-latest')));
  assert.ok(lines.some((l) => l.includes('images')));
});

test('a pinned snapshot is caught and the current id is not', () => {
  assert.equal(isRetired('text-moderation-007'), true);
  assert.equal(isRetired('text-moderation-stable'), true);
  assert.equal(isRetired('TEXT-MODERATION-LATEST'), true);
  assert.equal(isRetired('omni-moderation-latest'), false);
  assert.equal(isRetired('omni-moderation-2024-09-26'), false);
  assert.equal(isRetired(null), false);
  assert.deepEqual(retiredIds({ 'omni-moderation-latest': 5, 'text-moderation-007': 2 }),
                   ['text-moderation-007']);
  const mixed = ['proj_half', 4000, 3900,
                 { 'omni-moderation-latest': 3000, 'text-moderation-007': 900 }];
  const [state, detail] = classify(mixed);
  assert.equal(state, 'retired-model-id');
  assert.ok(detail.includes('23% of them'));
});

test('a low volume project is never graded', () => {
  const quiet = ['proj_scratch', 41, 0, {}];
  const [state, detail] = classify(quiet);
  assert.equal(state, 'below-floor');
  assert.ok(detail.includes('under the 500 floor'));
  assert.deepEqual(repairLines(state, quiet), []);
  assert.equal(classify(quiet, 10)[0], 'never-called');
});

test('a zero valued result row creates no entry', () => {
  const moderations = fold([bucket(row('proj_a', 'omni-moderation-latest', 0),
                                   row('proj_b', 'omni-moderation-latest', 7))]);
  assert.equal(moderations.proj_a, undefined);
  assert.equal(moderations.proj_b.requests, 7);
  assert.deepEqual(fold(null), {});
  assert.deepEqual(fold([{ results: null }]), {});
  assert.deepEqual(fold([bucket({ num_model_requests: 'not a number' })]), {});
  assert.ok('unattributed' in fold([bucket({ num_model_requests: 3 })]));
});

test('the ratio is graded as the soft signal it is', () => {
  const thin = ['proj_thin', 10000, 400, { 'omni-moderation-latest': 400 }];
  const [state, detail] = classify(thin);
  assert.equal(state, 'thin-coverage');
  assert.ok(detail.includes('ratio of 0.04'));
  assert.ok(repairLines(state, thin).some((l) => l.includes('cannot tell you which')));
  assert.equal(classify(thin, 500, 0.01)[0], 'covered');
  assert.deepEqual(repairLines('covered', thin), []);
});
