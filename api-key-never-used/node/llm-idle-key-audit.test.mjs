import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ageDays, anthropicVerdict, auditGaps, openaiVerdict, repairLines,
         revocationOrder, safeHint, seenKeyIds, windowStart }
  from './llm-idle-key-audit.mjs';

const NOW = new Date('2026-08-31T12:00:00Z');
const unix = (daysAgo) => Math.floor(NOW.getTime() / 1000) - daysAgo * 86400;

test('a key whose owner is perfectly fine is still the finding', () => {
  const key = { id: 'key_a1', name: 'vendor-trial', redacted_value: 'sk-...4f7a',
                created_at: unix(154), last_used_at: null,
                owner: { type: 'user', user: { email: 'dev@example.test' } },
                owner_project_access: 'active' };
  const [state, detail] = openaiVerdict(key, NOW);
  assert.equal(state, 'never-used');
  assert.match(detail, /154 day\(s\)/);
  assert.ok(repairLines(state, { container: 'proj_1', id: 'key_a1' })
    .some((line) => line.includes('cannot break traffic')));
});

test('the two providers answer different strengths of the question', () => {
  const [openaiState, openaiDetail] =
    openaiVerdict({ created_at: unix(200), last_used_at: null }, NOW);
  assert.equal(openaiState, 'never-used');
  assert.match(openaiDetail, /never used/);

  const [anthropicState, anthropicDetail] = anthropicVerdict(
    { id: 'apikey_z9', status: 'active', created_at: '2025-01-04T09:12:00Z' },
    new Set(), 30, NOW);
  assert.equal(anthropicState, 'unused-in-window');
  assert.match(anthropicDetail, /last 30 day\(s\)/);
  assert.match(anthropicDetail, /no last_used_at field/);
  assert.ok(!anthropicDetail.split('not a claim')[0].includes('never used'));
});

test('the two defaulted parameters are the audit and are asserted', () => {
  assert.deepEqual(auditGaps({ include_archived: 'true' },
                             { owner_project_access: 'any' }), []);
  const gaps = auditGaps({ limit: 100 }, { limit: 100 });
  assert.equal(gaps.length, 2);
  assert.ok(gaps.some((g) => g.includes('include_archived')));
  assert.ok(gaps.some((g) => g.includes('owner_project_access')));
  assert.equal(auditGaps({ include_archived: 'true' }, { limit: 100 }).length, 1);
});

test('dates arrive in two shapes and a zero is not 1970', () => {
  assert.equal(ageDays(unix(45), NOW), 45);
  assert.equal(ageDays('2026-08-01T00:00:00Z', NOW), 30);
  assert.equal(ageDays(String(unix(7)), NOW), 7);
  assert.equal(ageDays(null, NOW), null);
  assert.equal(ageDays('not a date', NOW), null);
  assert.equal(ageDays(true, NOW), null);
  assert.equal(openaiVerdict({ created_at: unix(100), last_used_at: 0 }, NOW)[0],
               'never-used');
});

test('dormant and never used are graded and ordered apart', () => {
  assert.equal(openaiVerdict({ created_at: unix(120), last_used_at: unix(3) }, NOW)[0],
               'in-use');
  const [state, detail] =
    openaiVerdict({ created_at: unix(900), last_used_at: unix(412) }, NOW);
  assert.equal(state, 'dormant');
  assert.match(detail, /412 day\(s\) ago/);
  assert.equal(openaiVerdict({ created_at: unix(9), last_used_at: null }, NOW)[0],
               'too-new');

  const order = revocationOrder([
    { state: 'dormant', idle: 412, name: 'nightly' },
    { state: 'in-use', idle: 1, name: 'prod' },
    { state: 'never-used', idle: 154, name: 'vendor-trial' },
    { state: 'unused-in-window', idle: 300, name: 'ingest' },
  ]);
  assert.deepEqual(order.map((r) => r.state),
                   ['never-used', 'unused-in-window', 'dormant']);
});

test('an anthropic key seen in the report is not a finding', () => {
  const seen = seenKeyIds([{ data: [{ results: [{ api_key_id: 'apikey_a' },
                                                { api_key_id: null },
                                                { api_key_id: 'apikey_b' }] }] }]);
  assert.deepEqual([...seen].sort(), ['apikey_a', 'apikey_b']);
  assert.equal(anthropicVerdict({ id: 'apikey_a', status: 'active',
                                  created_at: '2024-02-02T00:00:00Z' },
                                seen, 30, NOW)[0], 'seen-in-window');
  assert.equal(anthropicVerdict({ id: 'apikey_c', status: 'archived' },
                                seen, 30, NOW)[0], 'not-active');
  assert.equal(seenKeyIds([]).size, 0);
  assert.equal(seenKeyIds(null).size, 0);
});

test('no key value can reach the output', () => {
  assert.equal(safeHint('sk-...4f7a'), 'sk-...4f7a');
  assert.equal(safeHint('sk-ant-...igAA'), 'sk-ant-...igAA');
  assert.equal(safeHint('sk-abcd****wxyz'), 'sk-abcd****wxyz');
  assert.equal(safeHint('sk-fake-not-redacted-value'), '(hint withheld)');
  assert.equal(safeHint(`....${'x'.repeat(60)}`), '(hint withheld)');
  assert.equal(safeHint(null), '(no hint)');
  assert.equal(safeHint(''), '(no hint)');
});

test('the window start is floored to midnight utc', () => {
  assert.equal(windowStart(30, new Date('2026-08-31T17:45:12Z')),
               '2026-08-01T00:00:00Z');
});
