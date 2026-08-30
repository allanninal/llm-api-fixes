import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, mask, memberEmails, ownerGrant, projectRoles, repairLines,
         sentAt } from './openai-stale-invite-audit.mjs';

const NOW = 1780000000;
const DAY = 86400;

const ROSTER = memberEmails([{ email: 'Mel@example.com' }, { email: 'pat@example.com' }]);

const invite = (id, email, { role = 'reader', status = 'pending', sentDays = 137,
                             expiresDays = 107, projects = [] } = {}) =>
  ({ id, email, role, status, invited_at: NOW - sentDays * DAY,
     expires_at: expiresDays === null ? null : NOW - expiresDays * DAY, projects });

test('a pending invite past its expiry is the row a status filter misses', () => {
  const row = invite('invite_01hd', 'rob@example.com', { role: 'owner' });
  const [state, detail] = classify(row, ROSTER, NOW);
  assert.equal(state, 'expired-but-still-pending');
  assert.match(detail, /filter on status alone/);
  assert.match(detail, /107 day\(s\) ago/);

  const relabelled = { ...row, status: 'expired' };
  const [other, otherDetail] = classify(relabelled, ROSTER, NOW);
  assert.equal(other, 'expired-uncollected');
  assert.match(otherDetail, /never cleaned up/);
  assert.notDeepEqual(repairLines(state, row), repairLines(other, relabelled));
});

test('an invite for somebody already on the roster is not an onboarding failure', () => {
  const row = invite('invite_01me', 'mel@EXAMPLE.com', { sentDays: 61, expiresDays: 31 });
  const [state, detail] = classify(row, ROSTER, NOW);
  assert.equal(state, 'already-a-member');
  assert.match(detail, /already on the roster/);
  const lines = repairLines(state, row);
  assert.ok(lines.some((l) => l.includes('no onboarding problem here')));
  assert.ok(!lines.some((l) => l.includes('re-send')));
});

test('an owner grant hides inside the project entries', () => {
  const plain = invite('invite_01a', 'jo@example.com',
    { projects: [{ id: 'proj_web', role: 'member' }] });
  const hidden = invite('invite_01b', 'kim@example.com',
    { projects: [{ id: 'proj_ingest', role: 'owner' },
                 { id: 'proj_web', role: 'member' }] });
  assert.equal(ownerGrant(plain), false);
  assert.equal(ownerGrant(hidden), true);
  assert.equal(ownerGrant(invite('invite_01c', 'x@y.com', { role: 'owner' })), true);
  assert.deepEqual(projectRoles(hidden),
                   [['proj_ingest', 'owner'], ['proj_web', 'member']]);
  assert.deepEqual(projectRoles({}), []);
  const lines = repairLines('expired-but-still-pending', hidden);
  assert.ok(lines.some((l) => l.includes('offers owner rights')));
  assert.ok(lines.some((l) => l.includes('proj_ingest=owner')));
});

test('a stale but live invite is its own state', () => {
  const row = invite('invite_01j', 'jay@example.com', { sentDays: 29 });
  row.expires_at = NOW + 3 * DAY;
  const [state, detail] = classify(row, ROSTER, NOW);
  assert.equal(state, 'pending-stale');
  assert.match(detail, /29 day\(s\)/);
  assert.ok(repairLines(state, row).some((l) => l.includes('delivery status')));
});

test('a fresh invite and an accepted one are not findings', () => {
  const fresh = invite('invite_01f', 'new@example.com', { sentDays: 2 });
  fresh.expires_at = NOW + 5 * DAY;
  assert.equal(classify(fresh, ROSTER, NOW)[0], 'pending');
  assert.deepEqual(repairLines('pending', fresh), []);
  const done = invite('invite_01g', 'old@example.com', { status: 'accepted' });
  assert.equal(classify(done, ROSTER, NOW)[0], 'accepted');
  assert.equal(classify({ status: 'revoked', email: 'z@x.com' }, ROSTER, NOW)[0],
               'unknown-status');
});

test('the sent timestamp is read under either field name', () => {
  assert.equal(sentAt({ invited_at: 1700000000 }), 1700000000);
  assert.equal(sentAt({ created_at: 1700000001 }), 1700000001);
  assert.equal(sentAt({ invited_at: null, created_at: 1700000002 }), 1700000002);
  assert.equal(sentAt({ invited_at: 'not a date' }), null);
  assert.equal(sentAt({}), null);
  assert.equal(sentAt(null), null);
  const row = { id: 'i', email: 'q@x.com', role: 'reader', status: 'pending',
                expires_at: NOW - DAY };
  assert.equal(classify(row, ROSTER, NOW)[0], 'expired-but-still-pending');
});

test('every repair ends with the delete and masks the address', () => {
  const row = invite('invite_01hd', 'rob@example.com', { role: 'owner' });
  const lines = repairLines('expired-but-still-pending', row);
  assert.equal(lines[lines.length - 1],
               'DELETE /v1/organization/invites/invite_01hd');
  assert.equal(mask('rob@example.com'), 'r***@example.com');
  assert.equal(mask(null), 'unknown');
  assert.equal(memberEmails(null).size, 0);
});
