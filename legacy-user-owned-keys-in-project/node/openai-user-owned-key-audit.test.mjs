import { test } from 'node:test';
import assert from 'node:assert/strict';
import { foldCosts, migrationPlan, ownerKind, ownerLabel, projectNote,
         safeHint, spendLine, spendOf, verdict }
  from './openai-user-owned-key-audit.mjs';

const userKey = (id, name, email) => ({
  id, name, redacted_value: 'sk-...9c31',
  owner: { type: 'user', user: { id: 'user_1', email } },
  owner_project_access: 'active',
});

const serviceKey = (id, name) => ({
  id, name, redacted_value: 'sk-...aa02',
  owner: { type: 'service_account', service_account: { id: 'svc_1', name: 'ingest' } },
});

const costPage = (rows) => ({
  data: [{ results: rows.map(([api_key_id, value, currency]) =>
    ({ api_key_id, amount: { value, currency } })) }],
  has_more: false,
});

test('the share of the bill is not part of the verdict', () => {
  const key = userKey('key_1', 'api-main', 'marco@example.test');
  const tiny = verdict(key, 340.0, 2);
  const huge = verdict(key, 11402.88, 2);
  assert.equal(tiny[0], 'personal-key-in-production');
  assert.deepEqual(tiny, huge);

  const even = foldCosts([costPage([['key_1', 5000.0, 'USD'],
                                    ['key_2', 5000.0, 'USD']])]);
  assert.equal(verdict(userKey('key_1', 'api-main', 'm@example.test'),
                       spendOf(even, 'key_1'), 1)[0], 'personal-key-in-production');
  assert.equal(verdict(userKey('key_2', 'worker-2', 'd@example.test'),
                       spendOf(even, 'key_2'), 1)[0], 'personal-key-in-production');
});

test('a personal key with no traffic is a different repair', () => {
  const [state, detail] = verdict(userKey('key_9', 'scratch', 'm@example.test'), 0.0, 2);
  assert.equal(state, 'personal-key-idle');
  assert.match(detail, /revocation rather than a migration/);
  assert.equal(verdict(serviceKey('key_s', 'ingest'), 90000.0, 2)[0],
               'service-account-key');
});

test('an unrecognised owner is never folded into either camp', () => {
  assert.equal(ownerKind({ owner: { type: 'user' } }), 'user');
  assert.equal(ownerKind({ owner: { type: 'SERVICE_ACCOUNT' } }), 'service_account');
  assert.equal(ownerKind({ owner: { type: 'robot' } }), 'unknown');
  assert.equal(ownerKind({ owner: null }), 'unknown');
  assert.equal(ownerKind({}), 'unknown');
  assert.equal(ownerKind(null), 'unknown');
  const [state, detail] = verdict({ owner: { type: 'robot' } }, 4000.0, 3);
  assert.equal(state, 'unattributable-owner');
  assert.match(detail, /whose lifecycle/);
  assert.equal(ownerLabel(userKey('k', 'n', 'd@example.test')), 'd@example.test');
  assert.equal(ownerLabel(serviceKey('k', 'n')), 'ingest');
  assert.equal(ownerLabel({}), '(no owner block)');
});

test('an empty service account roster is a project level finding', () => {
  assert.equal(projectNote('proj_prod', 2, 0),
    'project proj_prod: no service accounts at all, and 2 user-owned key(s) are spending');
  assert.equal(projectNote('proj_prod', 2, 3), null);
  assert.equal(projectNote('proj_evals', 0, 0), null);
  const [state, detail] = verdict(userKey('key_1', 'api-main', 'm@example.test'), 9000.0, 0);
  assert.equal(state, 'personal-key-in-production');
  assert.match(detail, /no service accounts at all/);
});

test('two currencies are reported side by side and never added', () => {
  const costs = foldCosts([costPage([['key_1', 400.0, 'USD'],
                                     ['key_1', 300.0, 'USD'],
                                     ['key_1', 120.0, 'EUR']])]);
  assert.deepEqual(costs.key_1, { USD: 700.0, EUR: 120.0 });
  const line = spendLine(costs, 'key_1', 30);
  assert.equal(line, '120.00 EUR + 700.00 USD over 30 day(s)');
  assert.ok(!line.includes('820'));
  assert.equal(spendOf(costs, 'key_1'), 700.0);
  assert.equal(spendOf(costs, 'key_absent'), 0);
  assert.equal(spendLine(costs, 'key_absent', 30), 'no cost rows in 30 day(s)');
});

test('cost rows that cannot be read are skipped rather than guessed', () => {
  const costs = foldCosts([{ data: [{ results: [
    { api_key_id: null, amount: { value: 5.0, currency: 'USD' } },
    { api_key_id: 'key_1', amount: null },
    { api_key_id: 'key_1', amount: { value: 'many', currency: 'USD' } },
    { api_key_id: 'key_1', amount: { value: 12.5 } },
  ] }] }]);
  assert.deepEqual(costs, { key_1: { USD: 12.5 } });
  assert.deepEqual(foldCosts([]), {});
  assert.deepEqual(foldCosts(null), {});
});

test('the migration puts the revocation last', () => {
  const steps = migrationPlan('proj_prod', 'key_1', 'api-main');
  assert.equal(steps.length, 4);
  assert.match(steps[0], /service_accounts/);
  assert.match(steps[1], /returned exactly once/);
  assert.match(steps[2], /confirm the spend has moved off/);
  assert.ok(steps[3].startsWith('only then revoke'));
  assert.equal(safeHint('sk-...9c31'), 'sk-...9c31');
  assert.equal(safeHint('sk-fake-whole-value-here'), '(hint withheld)');
  assert.equal(safeHint(null), '(no hint)');
});
