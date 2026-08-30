import { test } from 'node:test';
import assert from 'node:assert/strict';
import { adminKeyOwners, humans, mask, ownerRatio, projectOwnerShare, repairLines,
         roleCounts, roleOf, unusedPrivilege, verdict }
  from './openai-owner-ratio-audit.mjs';

const NOW = 1780000000;

const user = (id, email, role = 'reader', {
  service = false, scim = false, lastUsed = NOW, added = 1700000000 } = {}) =>
  ({ id, email, role, is_service_account: service, is_scim_managed: scim,
     api_key_last_used_at: lastUsed, added_at: added });

const ROSTER = [
  user('u_1', 'ada@example.com', 'owner', { lastUsed: null }),
  user('u_2', 'mel@example.com', 'owner', { lastUsed: NOW - 214 * 86400 }),
  user('u_3', 'pat@example.com', 'owner'),
  user('u_4', 'sam@example.com', 'owner', { scim: true }),
  user('u_5', 'kim@example.com', 'owner', { scim: true }),
  user('u_6', 'rob@example.com', 'reader'),
  user('u_7', 'jo@example.com', 'reader'),
  user('sa_1', 'ingest@svc', 'owner', { service: true }),
  user('sa_2', 'batch@svc', 'owner', { service: true }),
  user('sa_3', 'evals@svc', 'owner', { service: true }),
];

test('service accounts never count toward the owner ratio', () => {
  assert.equal(Math.round(ownerRatio(roleCounts(ROSTER)) * 100) / 100, 0.8);
  const people = humans(ROSTER);
  assert.equal(people.length, 7);
  const counts = roleCounts(people);
  assert.deepEqual(counts, { owner: 5, reader: 2, other: 0 });
  const [state, detail] = verdict(counts);
  assert.equal(state, 'owner-majority');
  assert.match(detail, /5 of 7/);
});

test('a small organization is never graded', () => {
  const two = [user('u_1', 'a@x.com', 'owner'), user('u_2', 'b@x.com', 'owner')];
  const [state, detail] = verdict(roleCounts(humans(two)));
  assert.equal(state, 'too-few-members');
  assert.match(detail, /too few/);
  assert.deepEqual(repairLines(state), []);
});

test('everyone being an owner is its own state', () => {
  const roster = Array.from({ length: 6 },
    (_, i) => user(`u_${i}`, `p${i}@x.com`, 'owner'));
  const [state, detail] = verdict(roleCounts(humans(roster)));
  assert.equal(state, 'everyone-is-owner');
  assert.match(detail, /stopped existing/);
});

test('a high count at a low share says the ceiling is a convention', () => {
  const roster = [
    ...Array.from({ length: 6 }, (_, i) => user(`o_${i}`, `o${i}@x.com`, 'owner')),
    ...Array.from({ length: 34 }, (_, i) => user(`r_${i}`, `r${i}@x.com`, 'reader')),
  ];
  const [state, detail] = verdict(roleCounts(humans(roster)));
  assert.equal(state, 'owner-count-high');
  assert.match(detail, /convention rather than a platform rule/);
});

test('no recorded key use is a question and not a verdict', () => {
  assert.deepEqual(unusedPrivilege(ROSTER[0], NOW), [true, 'no API key use on record']);
  const [old, note] = unusedPrivilege(ROSTER[1], NOW);
  assert.equal(old, true);
  assert.match(note, /214 day\(s\) ago/);
  assert.equal(unusedPrivilege(ROSTER[2], NOW)[0], false);
  assert.equal(unusedPrivilege({ api_key_last_used_at: 'yesterday' }, NOW)[0], true);
});

test('scim-managed owners get a repair pointed somewhere else', () => {
  const owners = humans(ROSTER).filter((p) => roleOf(p) === 'owner');
  const scim = owners.filter((p) => p.is_scim_managed);
  assert.equal(scim.length, 2);
  const lines = repairLines('owner-majority', scim.length, 1, 3);
  assert.ok(lines.some((l) => l.includes('identity provider')
                              && l.includes('reverted at the next sync')));
  assert.ok(lines.some((l) => l.includes('Revoke the key before')));
  assert.ok(lines.some((l) => l.includes('org-level demotion alone')));
});

test('the admin key index reads the owner block and nothing else', () => {
  const keys = [
    { id: 'key_admin_1', name: 'ci', owner: { id: 'u_3', name: 'Pat', type: 'user' } },
    { id: 'key_admin_2', owner: { user: { id: 'u_9', email: 'x@y.com' } } },
    { id: 'key_admin_3', owner: {} },
  ];
  assert.deepEqual(adminKeyOwners(keys), { u_3: 'Pat', u_9: 'x@y.com' });
  assert.deepEqual(adminKeyOwners(null), {});
});

test('project roles are the second level', () => {
  const members = [{ id: 'u_1', role: 'owner' }, { id: 'u_2', role: 'owner' },
                   { id: 'u_3', role: 'owner' },
                   { id: 'sa_1', role: 'owner', is_service_account: true }];
  assert.deepEqual(projectOwnerShare(members), [3, 3, 1]);
  const [owners, total, ratio] = projectOwnerShare(
    [{ id: 'u_1', role: 'owner' }, { id: 'u_2', role: 'member' },
     { id: 'u_3', role: 'member' }]);
  assert.deepEqual([owners, total], [1, 3]);
  assert.equal(Math.round(ratio * 100) / 100, 0.33);
  assert.deepEqual(projectOwnerShare([]), [0, 0, 0]);
});

test('unknown roles are never counted as restricted', () => {
  assert.equal(roleOf({ role: 'OWNER' }), 'owner');
  assert.equal(roleOf({ role: 'reader' }), 'reader');
  assert.equal(roleOf({ role: 'billing' }), 'other');
  assert.equal(roleOf({}), 'other');
  assert.equal(ownerRatio({}), 0);
});

test('emails are masked', () => {
  assert.equal(mask('ada@example.com'), 'a***@example.com');
  assert.equal(mask('service-account'), 'service-account');
  assert.equal(mask(null), 'unknown');
});
