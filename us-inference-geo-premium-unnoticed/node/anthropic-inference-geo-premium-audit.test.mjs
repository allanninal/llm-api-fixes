import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fold, geoOf, premiumEstimate, residencyDefault, tokensOf, usShare,
         verdict } from './anthropic-inference-geo-premium-audit.mjs';

/** One result from the messages usage report. */
function result({ geo = 'us', workspace = 'wrkspc_01Qy', uncached = 100000000,
                  output = 8000000, cacheRead = 0, write5m = 0, write1h = 0 } = {}) {
  return {
    inference_geo: geo, workspace_id: workspace,
    uncached_input_tokens: uncached, output_tokens: output,
    cache_read_input_tokens: cacheRead,
    cache_creation: { ephemeral_5m_input_tokens: write5m,
                      ephemeral_1h_input_tokens: write1h },
  };
}

function page(...results) {
  return { data: [{ starting_at: '2026-08-01T00:00:00Z', results }], has_more: false };
}

test('the premium is inside the billed amount, not added to it', () => {
  assert.ok(Math.abs(premiumEstimate(1100.0, 1.0) - 100.0) < 1e-6);
  assert.ok(Math.abs(premiumEstimate(1100.0, 0.5) - 50.0) < 1e-6);
  assert.equal(premiumEstimate(1100.0, 0), 0);
  assert.equal(premiumEstimate(0, 1.0), 0);
  assert.equal(premiumEstimate(1100.0, 1.0, 1.0), 0);
});

test('a workspace default and a per-request parameter are two findings', () => {
  const totals = { us: 400000000, global: 8000000 };
  assert.equal(verdict(totals, 'us')[0], 'us-by-workspace-default');
  assert.equal(verdict(totals, 'global')[0], 'us-by-request');
  assert.equal(verdict(totals, 'unset')[0], 'us-unexplained');
  assert.match(verdict(totals, 'us')[1], /98% of 408\.0M priced token\(s\)/);
});

test('models that predate the parameter are not a finding', () => {
  assert.equal(verdict({ not_available: 50000000 }, 'unset')[0], 'geo-unsupported');
  assert.equal(verdict({ global: 50000000 }, 'us')[0], 'no-us-traffic');
  assert.equal(verdict({ us: 900 }, 'us')[0], 'low-volume');
});

test('a null geo is unspecified and never global', () => {
  assert.equal(geoOf({ inference_geo: null }), 'unspecified');
  assert.equal(geoOf({}), 'unspecified');
  assert.equal(geoOf({ inference_geo: 'US' }), 'us');
  assert.equal(geoOf({ inference_geo: 'global' }), 'global');
  assert.equal(geoOf({ inference_geo: 'not_available' }), 'not_available');
});

test('every priced category counts, including the nested cache writes', () => {
  assert.equal(tokensOf(result({ uncached: 10, output: 5, cacheRead: 3,
                                 write5m: 2, write1h: 1 })), 21);
  assert.equal(tokensOf({ uncached_input_tokens: 10, cache_creation: null }), 10);
  assert.equal(tokensOf({}), 0);
});

test('folding keeps workspaces and geos apart', () => {
  const folded = fold([page(
    result({ geo: 'us', uncached: 400000000, output: 0 }),
    result({ geo: 'global', uncached: 8000000, output: 0 }),
    result({ geo: 'us', workspace: 'wrkspc_02Zz', uncached: 1000000, output: 0 }),
  )]);
  assert.deepEqual(folded.wrkspc_01Qy, { us: 400000000, global: 8000000 });
  assert.deepEqual(folded.wrkspc_02Zz, { us: 1000000 });
  assert.ok(Math.abs(usShare(folded.wrkspc_01Qy) - 400 / 408) < 1e-9);
  assert.equal(usShare({}), 0);
});

test('residency is read from the nested block', () => {
  assert.equal(residencyDefault({ data_residency: { default_inference_geo: 'us' } }), 'us');
  assert.equal(residencyDefault({ data_residency: { default_inference_geo: 'global' } }),
               'global');
  assert.equal(residencyDefault({ data_residency: {} }), 'unset');
  assert.equal(residencyDefault({}), 'unset');
  assert.equal(residencyDefault(null), 'unset');
});
