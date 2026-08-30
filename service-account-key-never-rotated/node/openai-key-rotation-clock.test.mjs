import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ageDays, corroboration, groupByAccount, newestAndOldest, rotationPlan,
         rotationVerdict, serviceAccountId }
  from './openai-key-rotation-clock.mjs';

const NOW = new Date('2026-08-31T12:00:00Z');
const ACCOUNT = { id: 'svc_1', name: 'ingest-worker', created_at: 0 };
const unix = (daysAgo) => Math.floor(NOW.getTime() / 1000) - daysAgo * 86400;

const saKey = (id, daysAgo, account = 'svc_1') => ({
  id, created_at: unix(daysAgo),
  owner: { type: 'service_account',
           service_account: { id: account, name: 'ingest-worker' } },
});

test('the newest key is the clock and the oldest would lie', () => {
  const rotated = [saKey('key_new', 45), saKey('key_old', 731)];
  assert.deepEqual(newestAndOldest(rotated, NOW), [45, 731]);
  const [state, detail] = rotationVerdict(ACCOUNT, rotated, NOW);
  assert.ok(state !== 'single-stale-key' && state !== 'stale-key');
  assert.equal(state, 'unfinished-rotation');
  assert.match(detail, /newest key 45 day\(s\) old/);

  const finished = rotationVerdict(ACCOUNT, [saKey('key_new', 45)], NOW);
  assert.equal(finished[0], 'rotating');
});

test('the key count produces three different findings', () => {
  const single = rotationVerdict(ACCOUNT, [saKey('key_a', 731)], NOW);
  assert.equal(single[0], 'single-stale-key');
  assert.match(single[1], /it is the only one/);
  assert.ok(rotationPlan('proj_1', 'ingest-worker', true)
    .some((s) => s.includes('mint a second key first')));

  const bothOld = rotationVerdict(ACCOUNT, [saKey('key_a', 402), saKey('key_b', 500)], NOW);
  assert.equal(bothOld[0], 'stale-key');
  assert.match(bothOld[1], /across 2 key\(s\)/);
  assert.ok(!rotationPlan('proj_1', 'ingest-worker', false)
    .some((s) => s.includes('mint a second key first')));

  const halfway = rotationVerdict(ACCOUNT, [saKey('key_a', 12), saKey('key_b', 588)], NOW);
  assert.equal(halfway[0], 'unfinished-rotation');
  assert.match(halfway[1], /still live/);
});

test('an empty or unreachable audit log is never corroboration', () => {
  const unreachable = corroboration([], 'proj_1', false);
  assert.equal(unreachable[0], 'audit-unavailable');
  assert.match(unreachable[1], /silence is not evidence/);
  const empty = corroboration([], 'proj_1', true);
  assert.equal(empty[0], 'audit-unavailable');
  assert.match(empty[1], /nothing is being recorded/);
});

test('the audit log confirms a project and never an account', () => {
  const [state, detail] = corroboration(
    [{ type: 'api_key.created', project: { id: 'proj_other' } }], 'proj_1', true, 180);
  assert.equal(state, 'confirmed-at-project-level');
  assert.match(detail, /project-level fact/);
  assert.match(detail, /not the service account/);

  const [state2, detail2] = corroboration([
    { type: 'api_key.created', project: { id: 'proj_1' } },
    { type: 'api_key.created', project: { id: 'proj_1' } }], 'proj_1', true, 180);
  assert.equal(state2, 'creation-activity-in-window');
  assert.match(detail2, /neither confirms nor clears/);
});

test('a personal key is not counted towards a service account', () => {
  const grouped = groupByAccount([
    saKey('key_a', 731),
    { id: 'key_user', created_at: unix(2),
      owner: { type: 'user', user: { email: 'dev@example.test' } } },
    { id: 'key_odd', created_at: unix(2), owner: null },
  ]);
  assert.deepEqual(Object.keys(grouped), ['svc_1']);
  assert.equal(grouped.svc_1.length, 1);
  assert.equal(rotationVerdict(ACCOUNT, grouped.svc_1, NOW)[0], 'single-stale-key');
  assert.equal(serviceAccountId({ owner: { type: 'service_account' } }), null);
  assert.equal(serviceAccountId(null), null);
  assert.deepEqual(groupByAccount([]), {});
});

test('a service account with no keys and one too new to judge', () => {
  const empty = rotationVerdict(
    { id: 'svc_2', name: 'search-indexer', created_at: unix(300) }, [], NOW);
  assert.equal(empty[0], 'service-account-with-no-keys');
  assert.match(empty[1], /300 day\(s\) ago/);
  assert.equal(rotationVerdict(ACCOUNT, [saKey('key_a', 4)], NOW)[0], 'too-new');
  assert.equal(rotationVerdict(
    ACCOUNT, [{ id: 'key_a', created_at: null, owner: null }], NOW)[0], 'too-new');
});

test('ages are read from unix seconds only', () => {
  assert.equal(ageDays(unix(180), NOW), 180);
  assert.equal(ageDays(null, NOW), null);
  assert.equal(ageDays('', NOW), null);
  assert.equal(ageDays(true, NOW), null);
  assert.equal(ageDays('not a number', NOW), null);
});

test('the rotation plan revokes last and names the missing field', () => {
  const steps = rotationPlan('proj_prod', 'ingest-worker', false);
  assert.equal(steps.length, 3);
  assert.match(steps[0], /returned exactly once/);
  assert.match(steps[1], /last_used_at should stop advancing/);
  assert.ok(steps[2].startsWith('revoke the old key'));
  assert.match(steps[2], /no expires_at/);
});
