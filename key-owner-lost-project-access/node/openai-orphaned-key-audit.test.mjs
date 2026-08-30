import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ownerLabel, verdict } from './openai-orphaned-key-audit.mjs';

const NOW = 1_756_000_000;

const make = (over = {}) => ({
  id: 'key_abc',
  redacted_value: 'sk-proj-...aB3d',
  owner_project_access: 'active',
  last_used_at: NOW - 3600,
  owner: { type: 'user', user: { email: 'dev@example.com' } },
  ...over,
});

test('active owner is not a finding', () => {
  assert.equal(verdict(make(), NOW)[0], 'in-force');
});

test('inactive owner used today is production traffic', () => {
  const [state, detail] = verdict(make({ owner_project_access: 'inactive' }), NOW);
  assert.equal(state, 'serving');
  assert.match(detail, /re-issue/);
});

test('inactive owner long idle is orphaned not serving', () => {
  const [state, detail] = verdict(
    make({ owner_project_access: 'inactive', last_used_at: NOW - 90 * 86400 }), NOW);
  assert.equal(state, 'orphaned');
  assert.match(detail, /90 day\(s\)/);
});

test('inactive owner never used is the safe one', () => {
  const [state, detail] = verdict(
    make({ owner_project_access: 'inactive', last_used_at: null }), NOW);
  assert.equal(state, 'dormant');
  assert.match(detail, /revoke first/);
});

test('missing access field is never read as active', () => {
  const key = make();
  delete key.owner_project_access;
  const [state, detail] = verdict(key, NOW);
  assert.equal(state, 'unknown');
  assert.match(detail, /owner_project_access=any/);
});

test('unrecognised access value is not silently fine', () => {
  assert.equal(verdict(make({ owner_project_access: 'pending' }), NOW)[0], 'unknown');
});

test('a service account key is judged on the same field', () => {
  const key = make({
    owner_project_access: 'inactive',
    owner: { type: 'service_account', service_account: { name: 'batch-runner' } },
  });
  assert.equal(verdict(key, NOW)[0], 'serving');
  assert.equal(ownerLabel(key), 'batch-runner');
});

test('owner label prefers the email', () => {
  assert.equal(ownerLabel(make()), 'dev@example.com');
  assert.equal(ownerLabel({ owner: { type: 'user' } }), 'user');
  assert.equal(ownerLabel({}), 'unknown owner');
});

test('the hot window is a parameter not a constant', () => {
  const key = make({ owner_project_access: 'inactive', last_used_at: NOW - 20 * 86400 });
  assert.equal(verdict(key, NOW, 7)[0], 'orphaned');
  assert.equal(verdict(key, NOW, 30)[0], 'serving');
});
