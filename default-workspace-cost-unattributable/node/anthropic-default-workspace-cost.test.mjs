import { test } from 'node:test';
import assert from 'node:assert/strict';
import { amount, costByWorkspace, foldKeys, keyAttribution, playgroundShare,
         repairLines, unattributedShare, usageSplit, verdict, weigh, windowStart }
  from './anthropic-default-workspace-cost.mjs';

const cost = (workspaceId, value) =>
  ({ workspace_id: workspaceId, amount: value, currency: 'USD' });

const use = (apiKeyId, workspaceId, tokens) =>
  ({ api_key_id: apiKeyId, workspace_id: workspaceId, uncached_input_tokens: tokens });

const page = (results) => ({ data: [{ results }], has_more: false });

const key = (id, name, { scopeType = 'workspace', scopeWs = null, topWs = null,
                         status = 'active' } = {}) =>
  ({ id, name, status, scope: { type: scopeType, workspace_id: scopeWs },
     workspace_id: topWs });

const KEYS = [
  key('apikey_01aa', 'nightly-summaries', { scopeType: 'organization' }),
  key('apikey_01bb', 'ingest-worker'),
  key('apikey_01cc', 'eval-runner'),
  key('apikey_01dd', 'adam-scratch'),
  key('apikey_01ee', 'billing-team', { scopeWs: 'wrkspc_01' }),
];

test('the unallocated bucket is two causes and one of them moves', () => {
  const costs = costByWorkspace([page([cost(null, '15706.09'),
                                       cost('wrkspc_01', '17000.00'),
                                       cost('wrkspc_02', '8502.46')])]);
  const total = Math.round(Object.values(costs).reduce((a, v) => a + v, 0) * 100) / 100;
  const share = unattributedShare(costs);
  assert.equal(Math.round(share * 100) / 100, 0.38);
  const split = usageSplit([page([use(null, null, 900000),
                                  use('apikey_01bb', null, 9100000),
                                  use('apikey_01ee', 'wrkspc_01', 40000000)])]);
  const folded = foldKeys(KEYS);
  const [state, detail] = verdict(share, total, folded, split);
  assert.equal(state, 'movable-keys');
  assert.match(detail, /4 active key\(s\)/);
  const repairs = repairLines(state, folded, split);
  assert.ok(repairs.some((l) => l.includes('organization scope')));
  assert.ok(repairs.some((l) => l.includes('Console playground')));
  assert.ok(repairs.some((l) => l.includes('rate-limit override')));
});

test('playground traffic has no key to move', () => {
  const costs = costByWorkspace([page([cost(null, '15706.09'),
                                       cost('wrkspc_01', '25502.46')])]);
  const split = usageSplit([page([use(null, null, 9000000),
                                  use('apikey_01bb', null, 1000000)])]);
  assert.equal(Math.round(playgroundShare(split) * 100) / 100, 0.9);
  const total = Math.round(Object.values(costs).reduce((a, v) => a + v, 0) * 100) / 100;
  const [state, detail] = verdict(unattributedShare(costs), total, foldKeys(KEYS), split);
  assert.equal(state, 'console-playground');
  assert.match(detail, /no key can be moved/);
  assert.ok(!repairLines(state, foldKeys(KEYS), split)
    .some((l) => l.includes('recreate each key')));
});

test('the scope resolver prefers scope over the deprecated field', () => {
  assert.deepEqual(keyAttribution(key('k', 'n', { scopeType: 'organization' })),
                   ['organization-scoped', null]);
  assert.deepEqual(keyAttribution(key('k', 'n', { scopeWs: 'wrkspc_01' })),
                   ['named-workspace', 'wrkspc_01']);
  assert.deepEqual(keyAttribution(key('k', 'n', { topWs: 'wrkspc_09' })),
                   ['named-workspace', 'wrkspc_09']);
  assert.equal(keyAttribution(key('k', 'n', { scopeWs: 'wrkspc_01', topWs: 'wrkspc_09' }))[1],
               'wrkspc_01');
  assert.deepEqual(keyAttribution(key('k', 'n')), ['default-workspace', null]);
  assert.deepEqual(keyAttribution({}), ['default-workspace', null]);
  assert.equal(keyAttribution(key('k', 'n', { scopeType: 'service_account' }))[0],
               'unknown-scope');
});

test('a playground request in the default workspace is counted once', () => {
  const split = usageSplit([page([use(null, null, 1000)])]);
  assert.equal(split['console-playground'], 1000);
  assert.equal(split['default-workspace'], 0);
  assert.equal(playgroundShare(split), 1);
  assert.equal(playgroundShare({}), 0);
});

test('amount is a decimal string and null gets a sentinel', () => {
  assert.equal(amount({ amount: '1174.40' }), 1174.4);
  assert.equal(amount({ amount: null }), 0);
  assert.equal(amount({ amount: 'not money' }), 0);
  assert.equal(amount(null), 0);
  const rows = costByWorkspace([page([cost(null, '10.00'), cost(null, '5.00'),
                                      cost('wrkspc_01', '85.00')])]);
  assert.equal(rows['(default workspace)'], 15);
  assert.equal(Math.round(unattributedShare(rows) * 100) / 100, 0.15);
  assert.equal(unattributedShare({}), 0);
});

test('inactive keys never reach the migration list', () => {
  const folded = foldKeys([...KEYS,
    key('apikey_01ff', 'retired', { status: 'inactive' }),
    key('apikey_01gg', 'gone', { status: 'archived' })]);
  const ids = folded['default-workspace'].map((k) => k.id);
  assert.ok(!ids.includes('apikey_01ff') && !ids.includes('apikey_01gg'));
  assert.equal(folded['default-workspace'].length, 3);
  assert.equal(folded['organization-scoped'].length, 1);
  assert.equal(folded['named-workspace'].length, 1);
  assert.deepEqual(foldKeys(null)['default-workspace'], []);
});

test('a small share and an empty window are never findings', () => {
  const costs = costByWorkspace([page([cost(null, '40.00'),
                                       cost('wrkspc_01', '960.00')])]);
  assert.equal(verdict(unattributedShare(costs), 1000, foldKeys(KEYS),
                       usageSplit([]))[0], 'attributed');
  assert.equal(verdict(1, 0, foldKeys([]), {})[0], 'no-spend-yet');
  assert.deepEqual(repairLines('attributed', {}, {}), []);
  assert.equal(weigh({ uncached_input_tokens: 10, output_tokens: 5,
                       cache_creation: { ephemeral_5m_input_tokens: 7 } }), 22);
  assert.equal(weigh({ cache_creation: 3 }), 0);
  assert.equal(weigh(null), 0);
});

test('every active key is placed and the bucket still has spend', () => {
  const folded = foldKeys([key('apikey_01ee', 'billing-team', { scopeWs: 'wrkspc_01' })]);
  const [state, detail] = verdict(0.31, 41208.55, folded,
    usageSplit([page([use('apikey_01ee', null, 5)])]));
  assert.equal(state, 'unattributable-no-key-to-move');
  assert.match(detail, /since been deleted/);
  assert.ok(repairLines(state, folded, {})
    .some((l) => l.includes('do not open a migration ticket')));
});

test('the window start is floored to midnight utc', () => {
  assert.equal(windowStart(30, new Date('2026-08-31T17:45:12Z')),
               '2026-08-01T00:00:00Z');
});
