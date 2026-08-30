import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, fold, unresolved, verdict }
  from './openai-tenant-attribution-audit.mjs';

const DIRECTORY = {
  user_eng1: { name: 'an engineer', service_account: false },
  user_eng2: { name: 'another engineer', service_account: false },
  sa_prod: { name: 'prod-backend', service_account: true },
};

function folded({ users, keys, projects, requests = 100000 } = {}) {
  return {
    users: users ?? { sa_prod: 100000 },
    keys: keys ?? { key_abc: 100000 },
    projects: projects ?? { proj_1: 100000 },
    requests,
  };
}

/** One daily bucket from the usage endpoint, grouped three ways. */
function bucket(rows) {
  return {
    data: [{
      start_time: 0,
      results: rows.map(([u, k, p, n]) => ({
        user_id: u, api_key_id: k, project_id: p, num_model_requests: n,
      })),
    }],
  };
}

test('every principal is one of your own and that is the finding', () => {
  const [state, detail] = verdict(
    folded({ users: { sa_prod: 90000, user_eng1: 10000 },
             keys: { key_a: 60000, key_b: 40000 } }),
    DIRECTORY, 412);
  assert.equal(state, 'keys-below-tenants');
  assert.match(detail, /2 distinct api_key_id value/);
  assert.match(detail, /org members or service accounts rather than customers/);
});

test('one key is its own worst case', () => {
  const [state, detail] = verdict(folded(), DIRECTORY, 412);
  assert.equal(state, 'single-key');
  assert.match(detail, /one bucket/);
});

test('enough keys means the platform can slice', () => {
  const keys = {};
  for (let i = 0; i < 500; i += 1) keys[`key_${i}`] = 10;
  assert.equal(verdict(folded({ keys }), DIRECTORY, 412)[0], 'segmented');
});

test('without a tenant count the script does not invent a verdict', () => {
  const [state, detail] = verdict(folded({ keys: { key_a: 5, key_b: 5 } }), DIRECTORY);
  assert.equal(state, 'unknown-tenant-count');
  assert.match(detail, /Pass the tenant count/);
  assert.equal(
    verdict({ users: {}, keys: {}, projects: {}, requests: 0 }, DIRECTORY)[0],
    'no-usage');
});

test('a principal the directory does not know is a different problem', () => {
  const f = folded({ users: { user_departed: 5000, sa_prod: 5000 },
                     keys: { key_a: 5000, key_b: 5000 } });
  assert.equal(classify('user_departed', DIRECTORY), 'unresolved');
  assert.equal(classify('sa_prod', DIRECTORY), 'service-account');
  assert.equal(classify('user_eng1', DIRECTORY), 'member');
  assert.deepEqual(unresolved(f, DIRECTORY), ['user_departed']);
  assert.match(verdict(f, DIRECTORY, 412)[1], /resolve to nobody/);
});

test('fold counts the three dimensions and skips the nulls', () => {
  const f = fold([bucket([['sa_prod', 'key_a', 'proj_1', 700],
                          [null, 'key_b', 'proj_1', 300]])]);
  assert.equal(f.requests, 1000);
  assert.deepEqual(f.users, { sa_prod: 700 });
  assert.deepEqual(f.keys, { key_a: 700, key_b: 300 });
  assert.deepEqual(f.projects, { proj_1: 1000 });
});
