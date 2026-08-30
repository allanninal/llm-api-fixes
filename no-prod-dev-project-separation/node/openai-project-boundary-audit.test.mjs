import { test } from 'node:test';
import assert from 'node:assert/strict';
import { active, environments, mixed, repairLines, shares, spendByProject,
         verdict, windowStart } from './openai-project-boundary-audit.mjs';

const project = (id, name, status = 'active', archivedAt = null) =>
  ({ id, name, status, archived_at: archivedAt });

const cost = (projectId, value) =>
  ({ project_id: projectId, amount: { value, currency: 'usd' } });

const buckets = (...results) => [{ results }];

test('one active project is the finding whatever the bill says', () => {
  const live = active([project('proj_a', 'Default project'),
                       project('proj_old', 'Prototype', 'archived')]);
  assert.equal(live.length, 1);
  const ranked = shares(spendByProject(buckets(cost('proj_a', 18406.11))));
  const [state, detail] = verdict(live.length, ranked);
  assert.equal(state, 'no-boundary');
  assert.match(detail, /no second container/);
  const repairs = repairLines(state, new Set(['prod', 'ci', 'local']));
  assert.ok(repairs.some((l) => l.includes('archived but never deleted')));
  assert.ok(repairs.some((l) => l.includes('key names')));
});

test('a dominant project in a split org is the other note', () => {
  const ranked = shares(spendByProject(buckets(
    cost('proj_prod', 96000), cost('proj_stage', 2400),
    cost('proj_dev', 1100), cost('proj_ci', 500))));
  const [state, detail] = verdict(4, ranked);
  assert.equal(state, 'concentration-not-topology');
  assert.match(detail, /different repair/);
  assert.ok(repairLines(state).some((l) => l.includes('Rank the cost rows')));
});

test('projects that exist and never receive traffic', () => {
  const ranked = shares(spendByProject(buckets(
    cost('proj_prod', 9900), cost('proj_stage', 0), cost('proj_dev', 0))));
  const [state, detail] = verdict(3, ranked);
  assert.equal(state, 'boundary-unused');
  assert.match(detail, /no traffic routes to them/);
});

test('archived projects are dropped on either signal', () => {
  const rows = [project('a', 'live'), project('b', 'by status', 'archived'),
                project('c', 'by timestamp', 'active', 1700000000),
                project('d', 'shouty', 'ARCHIVED')];
  assert.deepEqual(active(rows).map((p) => p.id), ['a']);
  assert.deepEqual(active(null), []);
});

test('an ungrouped row is never ranked as a project', () => {
  const spend = spendByProject(buckets(cost(null, 41000), cost('proj_a', 900),
                                       cost('proj_b', 100)));
  assert.equal(spend.ungrouped, 41000);
  const ranked = shares(spend);
  assert.deepEqual(ranked.map((r) => r[0]), ['proj_a', 'proj_b']);
  assert.equal(Math.round(ranked[0][2] * 100) / 100, 0.9);
  assert.equal(verdict(3, ranked)[0], 'separated');
});

test('environment words match whole tokens only', () => {
  assert.deepEqual([...environments('prod-worker')], ['prod']);
  assert.deepEqual([...environments('Local Adam')], ['local']);
  assert.deepEqual([...environments('devops-runner')], []);
  assert.deepEqual([...environments('provider-proxy')], []);
  assert.deepEqual([...environments('protest')], []);
  assert.deepEqual([...environments(null)], []);
  assert.deepEqual([...mixed(['prod-worker', 'local-adam', 'ci-fixtures'])].sort(),
                   ['ci', 'local', 'prod']);
});

test('no spend and no projects are never verdicts', () => {
  assert.equal(verdict(0, [])[0], 'no-active-projects');
  const [state, detail] = verdict(3, shares(spendByProject(buckets(cost('a', 0.2)))));
  assert.equal(state, 'no-spend-yet');
  assert.match(detail, /nothing has tested it/);
  assert.deepEqual(repairLines('separated'), []);
  assert.deepEqual(spendByProject(null), {});
  assert.deepEqual(shares(null), []);
});

test('the window starts at midnight utc', () => {
  assert.equal(windowStart(30, new Date('2026-08-31T17:45:12Z')),
               Date.UTC(2026, 7, 1) / 1000);
});
