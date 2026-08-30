import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fold, isUnsupported, orgHasPriority, repairLines, share, tier,
         verdict, weigh, windowStart }
  from './anthropic-priority-tier-coverage.mjs';

const result = (model, serviceTier, tokens) =>
  ({ model, service_tier: serviceTier, uncached_input_tokens: tokens });

const page = (results) => ({ data: [{ results }], has_more: false });

const COVERED_ORG = [page([
  result('claude-opus-5', 'standard', 812_400_000),
  result('claude-opus-4-5', 'priority', 41_800_000),
  result('claude-opus-4-5', 'standard', 4_100_000),
])];

test('a model that never reports priority has no coverage', () => {
  const rows = fold(COVERED_ORG);
  assert.equal(orgHasPriority(rows), true);
  const [state, detail] = verdict('claude-opus-5', rows['claude-opus-5'], true);
  assert.equal(state, 'unsupported-model');
  assert.match(detail, /not supported by Priority Tier/);
  assert.equal(verdict('claude-opus-4-5', rows['claude-opus-4-5'], true)[0],
               'priority-covered');
  assert.ok(repairLines(state, 'claude-opus-5')
    .some((line) => line.includes('coverage, not configuration')));
});

test('an org with no commitment is not a per-model finding', () => {
  const rows = fold([page([
    result('claude-opus-5', 'standard', 812_400_000),
    result('claude-opus-4-5', 'standard', 45_900_000),
  ])]);
  assert.equal(orgHasPriority(rows), false);
  for (const model of Object.keys(rows)) {
    const [state, detail] = verdict(model, rows[model], false);
    assert.equal(state, 'no-priority-in-org');
    assert.match(detail, /without a capacity commitment/);
    assert.deepEqual(repairLines(state, model), []);
  }
});

test('the exclusion list matches families and not neighbours', () => {
  assert.equal(isUnsupported('claude-opus-5'), true);
  assert.equal(isUnsupported('claude-sonnet-5-20260101'), true);
  assert.equal(isUnsupported('claude-mythos-5'), true);
  assert.equal(isUnsupported('claude-mythos-preview'), true);
  assert.equal(isUnsupported('claude-opus-4-5'), false);
  assert.equal(isUnsupported('claude-haiku-4-5-20251001'), false);
  assert.equal(isUnsupported('claude-sonnet-4-6'), false);
  assert.equal(isUnsupported('claude-fable-5'), false);
  assert.equal(isUnsupported(null), false);
});

test('a model off the list with zero priority is a different finding', () => {
  const rows = fold([page([
    result('claude-haiku-4-5-20251001', 'standard', 240_000_000),
    result('claude-opus-4-5', 'priority', 40_000_000),
  ])]);
  const [state, detail] = verdict('claude-haiku-4-5-20251001',
    rows['claude-haiku-4-5-20251001'], true);
  assert.equal(state, 'uncovered-model');
  assert.match(detail, /not on the documented exclusion list/);
});

test('a thin priority share is a sizing finding', () => {
  const rows = fold([page([
    result('claude-haiku-4-5-20251001', 'priority', 14_000_000),
    result('claude-haiku-4-5-20251001', 'standard', 86_000_000),
  ])]);
  const [state, detail] = verdict('claude-haiku-4-5-20251001',
    rows['claude-haiku-4-5-20251001'], true);
  assert.equal(state, 'partial-priority');
  assert.match(detail, /14% priority/);
  assert.ok(repairLines(state, 'claude-haiku-4-5-20251001')
    .some((line) => line.includes('burndown')));
});

test('cache_creation is an object and all of it counts', () => {
  assert.equal(weigh({ uncached_input_tokens: 100, cache_read_input_tokens: 10,
                       output_tokens: 5,
                       cache_creation: { ephemeral_5m_input_tokens: 40,
                                         ephemeral_1h_input_tokens: 20 } }), 175);
  assert.equal(weigh({ uncached_input_tokens: 'not a number' }), 0);
  assert.equal(weigh({ cache_creation: 12 }), 0);
  assert.equal(weigh(null), 0);
});

test('an absent service_tier never lands in standard', () => {
  assert.equal(tier({ service_tier: 'priority' }), 'priority');
  assert.equal(tier({ service_tier: 'BATCH' }), 'batch');
  assert.equal(tier({}), 'unknown');
  assert.equal(tier({ service_tier: 'flex' }), 'unknown');
  const rows = fold([page([result('claude-opus-5', null, 5_000_000)])]);
  assert.equal(rows['claude-opus-5'].standard, 0);
  assert.equal(rows['claude-opus-5'].unknown, 5_000_000);
  assert.equal(share(rows['claude-opus-5'], 'standard'), 0);
});

test('too little traffic is never a verdict', () => {
  const rows = fold([page([result('claude-opus-5', 'standard', 900)])]);
  const [state, detail] = verdict('claude-opus-5', rows['claude-opus-5'], true);
  assert.equal(state, 'low-volume');
  assert.match(detail, /too few to conclude/);
  assert.deepEqual(fold([]), {});
  assert.deepEqual(fold(null), {});
  assert.equal(orgHasPriority({}), false);
});

test('the window start is floored to midnight utc', () => {
  assert.equal(windowStart(30, new Date('2026-08-31T17:45:12Z')),
               '2026-08-01T00:00:00Z');
  assert.equal(windowStart(0, new Date('2026-08-31T17:45:12Z')),
               '2026-08-31T00:00:00Z');
});
