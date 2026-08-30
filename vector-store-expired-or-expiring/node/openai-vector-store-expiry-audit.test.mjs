import { test } from 'node:test';
import assert from 'node:assert/strict';
import { anchorNote, driftSeconds, expiryAt, expiryState, idSet, idleSeconds,
         policy, repairLines } from './openai-vector-store-expiry-audit.mjs';

const NOW = 1800000000;
const DAY = 86400;

const store = ({ id = 'vs_a1', name = 'handbook', status = 'completed',
                 days = null, anchor = 'last_active_at', expiresAt = null,
                 lastActiveAt = null, usageBytes = 41000000 } = {}) => {
  const row = { id, name, status, usage_bytes: usageBytes,
                last_active_at: lastActiveAt, expires_at: expiresAt,
                file_counts: { total: 9, completed: 9, failed: 0,
                               in_progress: 0, cancelled: 0 } };
  if (days !== null) row.expires_after = { anchor, days };
  return row;
};

test('an expired store has no repair that touches the policy', () => {
  const dead = store({ status: 'expired', days: 7, expiresAt: NOW - 84 * DAY });
  const [state, detail] = expiryState(dead, NOW);
  assert.equal(state, 'expired');
  assert.match(detail, /84 day\(s\) ago/);
  assert.match(detail, /not recoverable/);
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('re-ingest into a new store')));
  assert.ok(!lines.some((l) => l.includes('clear it by updating')));
});

test('the same timer is a finding only on a store you called permanent', () => {
  const live = store({ id: 'vs_a1', days: 7, expiresAt: NOW + 2 * DAY,
                       lastActiveAt: NOW - 5 * DAY });
  const temp = store({ id: 'vs_e5', name: 'session-uploads', days: 7,
                       expiresAt: NOW + 2 * DAY, lastActiveAt: NOW - 5 * DAY });
  assert.equal(expiryState(live, NOW, new Set(['vs_a1']))[0], 'policy-on-permanent');
  assert.equal(expiryState(temp, NOW, new Set(['vs_a1']))[0], 'expiring-soon');
  assert.ok(repairLines('policy-on-permanent')
    .some((l) => l.includes('has to be no policy at all')));
});

test('the reported expiry wins and the drift is only printed', () => {
  const drifting = store({ days: 7, lastActiveAt: NOW - 5 * DAY,
                           expiresAt: NOW + 2 * DAY + 3 * 3600 });
  assert.equal(driftSeconds(drifting), 3 * 3600);
  const left = (expiryAt(drifting) - NOW) / DAY;
  assert.ok(left > 2.1 && left < 2.2);
  assert.equal(expiryState(drifting, NOW, new Set(), 7)[0], 'expiring-soon');
  assert.equal(driftSeconds(store({ days: 7 })), null);
  assert.equal(driftSeconds(store({ expiresAt: NOW })), null);
});

test('the anchor is only mentioned when it is not the documented one', () => {
  assert.equal(anchorNote(store({ days: 7 })), null);
  assert.equal(anchorNote(store()), null);
  const note = anchorNote(store({ days: 7, anchor: 'created_at' }));
  assert.match(note, /created_at/);
  assert.match(note, /last_active_at/);
});

test('a policy with no usable day count reads as no policy', () => {
  assert.deepEqual(policy(store({ days: 7 })), ['last_active_at', 7]);
  assert.equal(policy(store()), null);
  assert.equal(policy({ expires_after: { anchor: 'last_active_at' } }), null);
  assert.equal(policy({ expires_after: { anchor: 'last_active_at', days: 0 } }), null);
  assert.equal(policy({ expires_after: '7 days' }), null);
  assert.equal(policy(null), null);
});

test('a store with no policy is reported as a bill not a pass', () => {
  const [state, detail] = expiryState(store({ usageBytes: 43200512 }), NOW);
  assert.equal(state, 'permanent');
  assert.match(detail, /41\.2 MiB retained and billed/);
  assert.ok(repairLines(state).some((l) => l.includes('billed by the hour')));
});

test('the clock helpers tolerate a missing field', () => {
  assert.equal(idleSeconds(store({ lastActiveAt: NOW - 3 * DAY }), NOW), 3 * DAY);
  assert.equal(idleSeconds(store(), NOW), null);
  assert.equal(expiryAt(store()), null);
  assert.equal(expiryAt({ expires_at: 'soon' }), null);
  assert.deepEqual([...idSet('vs_a1, vs_b2', ['vs_a1'])].sort(), ['vs_a1', 'vs_b2']);
  assert.equal(idSet(null).size, 0);
  const far = store({ days: 90, expiresAt: NOW + 60 * DAY,
                      lastActiveAt: NOW - 30 * DAY });
  assert.equal(expiryState(far, NOW)[0], 'scheduled');
});
