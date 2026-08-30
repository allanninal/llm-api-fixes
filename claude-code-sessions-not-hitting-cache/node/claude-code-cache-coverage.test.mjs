import { test } from 'node:test';
import assert from 'node:assert/strict';
import { actorName, costCents, costPerSession, dayStrings, fold, mask,
         readShare, repairLines, tokensOf, verdict }
  from './claude-code-cache-coverage.mjs';

const breakdown = (model, input, cacheRead = 0, cacheCreation = 0, cents = 0) => ({
  model,
  tokens: { input, output: 12_000, cache_read: cacheRead,
            cache_creation: cacheCreation },
  estimated_cost: { currency: 'USD', amount: cents },
});

const record = (email, sessions, entries) => ({
  date: '2026-08-30',
  actor: { type: 'user_actor', email_address: email },
  core_metrics: { num_sessions: sessions,
                  lines_of_code: { added: 90, removed: 12 },
                  commits_by_claude_code: 2 },
  model_breakdown: entries,
});

const page = (records) => ({ data: records, has_more: false });

test('two developers on the same work and one never reads a prefix', () => {
  const rows = fold([page([
    record('nobody@example.com', 11, [breakdown('claude-opus-5', 2_000_000, 0, 0, 4120)]),
    record('someone@example.com', 4,
      [breakdown('claude-opus-5', 300_000, 1_600_000, 200_000, 940)]),
  ])]);

  const [state, detail] = verdict(rows['nobody@example.com']);
  assert.equal(state, 'no-cache-at-all');
  assert.match(detail, /11 session/);
  assert.ok(repairLines(state).some((l) => l.includes('turns of the same session')));

  const [good, goodDetail] = verdict(rows['someone@example.com']);
  assert.equal(good, 'cached');
  assert.match(goodDetail, /84% of input read from cache/);
});

test('a single session zero is arithmetic not a finding', () => {
  const rows = fold([page([
    record('once@example.com', 1, [breakdown('claude-opus-5', 2_000_000, 0, 0, 900)]),
  ])]);
  const [state, detail] = verdict(rows['once@example.com']);
  assert.equal(state, 'too-few-sessions');
  assert.match(detail, /no earlier turn/);
  assert.deepEqual(repairLines(state), []);
});

test('written and never matched is the more expensive zero', () => {
  const rows = fold([page([
    record('churn@example.com', 6,
      [breakdown('claude-opus-5', 900_000, 0, 2_100_000, 5890)]),
  ])]);
  const [state, detail] = verdict(rows['churn@example.com']);
  assert.equal(state, 'writes-never-read');
  assert.match(detail, /2.1M token/);
  assert.ok(repairLines(state).some((l) => l.includes('worse than not caching at all')));
});

test('the whole model breakdown is summed and not just the first entry', () => {
  const rows = fold([page([
    record('two@example.com', 5, [
      breakdown('claude-opus-5', 1_000_000, 500_000, 0, 1000),
      breakdown('claude-haiku-4-5-20251001', 400_000, 100_000, 0, 250),
    ]),
  ])]);
  const row = rows['two@example.com'];
  assert.equal(row.input, 1_400_000);
  assert.equal(row.cache_read, 600_000);
  assert.equal(row.cents, 1250);
  assert.deepEqual([...row.models].sort(),
    ['claude-haiku-4-5-20251001', 'claude-opus-5']);
  assert.equal(costPerSession(row), 250);
});

test('sessions and cost accumulate across days', () => {
  const day = [record('daily@example.com', 3,
    [breakdown('claude-opus-5', 500_000, 0, 0, 600)])];
  const rows = fold([page(day), page(day), page(day)]);
  assert.equal(rows['daily@example.com'].sessions, 9);
  assert.equal(rows['daily@example.com'].days, 3);
  assert.equal(rows['daily@example.com'].cents, 1800);
});

test('both actor shapes are read and neither is handled', () => {
  assert.equal(actorName({ actor: { type: 'user_actor',
                                    email_address: 'a@example.com' } }),
               'a@example.com');
  assert.equal(actorName({ actor: { type: 'api_actor', api_key_name: 'ci-runner' } }),
               'ci-runner');
  assert.equal(actorName({ actor: {} }), 'unattributed');
  assert.equal(actorName({}), 'unattributed');
  assert.equal(actorName(null), 'unattributed');
});

test('an email address is masked before it is printed', () => {
  assert.equal(mask('someone@example.com'), 's***@example.com');
  assert.equal(mask('ci-runner'), 'ci-runner');
  assert.equal(mask(''), 'unattributed');
  assert.equal(mask(null), 'unattributed');
});

test('reads over reads plus input and writes are not a hit', () => {
  assert.equal(readShare({ cache_read: 900, input: 100 }), 0.9);
  assert.equal(readShare({ cache_read: 0, input: 100, cache_creation: 900 }), 0);
  assert.equal(readShare({}), 0);
  assert.equal(costPerSession({ sessions: 0, cents: 100 }), null);
  assert.deepEqual(tokensOf(null),
    { input: 0, output: 0, cache_read: 0, cache_creation: 0 });
  assert.equal(tokensOf({ tokens: { input: 'x' } }).input, 0);
  assert.equal(costCents({ estimated_cost: { amount: '12.50' } }), 12.5);
  assert.equal(costCents({ estimated_cost: { amount: 'not money' } }), 0);
});

test('today is never requested because today is always partial', () => {
  assert.deepEqual(dayStrings(3, new Date('2026-08-31T09:00:00Z')),
    ['2026-08-30', '2026-08-29', '2026-08-28']);
  assert.deepEqual(dayStrings(1, new Date('2026-01-01T00:30:00Z')), ['2025-12-31']);
  assert.deepEqual(fold([]), {});
  assert.deepEqual(fold(null), {});
});
