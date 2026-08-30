import { test } from 'node:test';
import assert from 'node:assert/strict';
import { band, cachedShare, fold, longShare, uncachedCost, verdict }
  from './anthropic-long-context-audit.mjs';

/** One result from the messages usage report. */
function result({ window = '200k-1M', model = 'claude-opus-5',
                  uncached = 400000000, cacheRead = 0 } = {}) {
  return {
    context_window: window, model,
    uncached_input_tokens: uncached, cache_read_input_tokens: cacheRead,
  };
}

function page(...results) {
  return { data: [{ starting_at: '2026-08-01T00:00:00Z', results }], has_more: false };
}

/** A folded model row shaped like fold() returns them. */
function rows({ longUncached = 400000000, longRead = 0,
                shortUncached = 160000000, unbanded = 0 } = {}) {
  const out = {
    '200k-1M': { uncached: longUncached, cache_read: longRead },
    '0-200k': { uncached: shortUncached, cache_read: 0 },
  };
  if (unbanded) out.unbanded = { uncached: unbanded, cache_read: 0 };
  return out;
}

test('a null context_window is unbanded and not the short band', () => {
  assert.equal(band({ context_window: null }), 'unbanded');
  assert.equal(band({}), 'unbanded');
  assert.equal(band({ context_window: '200k-1M' }), '200k-1M');
  assert.equal(band({ context_window: '0-200k' }), '0-200k');
  const withNulls = rows({ unbanded: 400000000 });
  assert.ok(Math.abs(longShare(withNulls) - 400 / 560) < 1e-9);
  const [state, detail] = verdict(withNulls);
  assert.equal(state, 'long-context-uncached');
  assert.match(detail, /71% of banded uncached input/);
});

test('a cached long prefix is a different state with a different sentence', () => {
  const [state, detail] = verdict(rows({ longUncached: 40000000,
                                         longRead: 360000000,
                                         shortUncached: 10000000 }));
  assert.equal(state, 'long-context-cached');
  assert.match(detail, /It is still just as long/);
});

test('a short context workload is not a finding', () => {
  assert.equal(verdict(rows({ longUncached: 10000000, shortUncached: 400000000 }))[0],
               'short-context');
  assert.equal(verdict(rows({ longUncached: 100, shortUncached: 100 }))[0], 'low-volume');
});

test('traffic the report never banded is reported as such', () => {
  const [state, detail] = verdict({ unbanded: { uncached: 400000000, cache_read: 0 } });
  assert.equal(state, 'unbanded-only');
  assert.match(detail, /cannot be placed in a band/);
});

test('the cached share is read inside the band', () => {
  assert.equal(cachedShare({ uncached: 0, cache_read: 100 }), 1);
  assert.equal(cachedShare({ uncached: 100, cache_read: 0 }), 0);
  assert.equal(cachedShare({ uncached: 50, cache_read: 50 }), 0.5);
  assert.equal(cachedShare({}), 0);
});

test('the rate is supplied rather than baked in', () => {
  assert.equal(uncachedCost(408000000, 5.0), 2040);
  assert.equal(uncachedCost(0, 5.0), 0);
  assert.equal(uncachedCost(1000000, 0), 0);
});

test('folding keeps models and bands apart', () => {
  const folded = fold([page(
    result({ window: '200k-1M', uncached: 200000000 }),
    result({ window: '200k-1M', uncached: 200000000, cacheRead: 5000000 }),
    result({ window: '0-200k', uncached: 160000000 }),
    result({ window: null, model: 'claude-haiku-4-5-20251001', uncached: 9000000 }),
  )]);
  assert.equal(folded['claude-opus-5']['200k-1M'].uncached, 400000000);
  assert.equal(folded['claude-opus-5']['200k-1M'].cache_read, 5000000);
  assert.equal(folded['claude-opus-5']['0-200k'].uncached, 160000000);
  assert.equal(folded['claude-haiku-4-5-20251001'].unbanded.uncached, 9000000);
});
