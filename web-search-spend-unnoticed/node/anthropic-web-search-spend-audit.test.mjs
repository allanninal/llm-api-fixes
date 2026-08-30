import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fee, fold, reconcile, searchSpend, verdict }
  from './anthropic-web-search-spend-audit.mjs';

/** One page of GET /v1/organizations/usage_report/messages. */
function page(...results) {
  return { data: [{ starting_at: '2026-08-01T00:00:00Z', results }], has_more: false };
}

/** One usage result. server_tool_use is nested beside the token fields. */
function usage({ key = 'apikey_01Rs', searches = null, tools = {} } = {}) {
  const use = { ...tools };
  if (searches !== null) use.web_search_requests = searches;
  const row = { api_key_id: key, uncached_input_tokens: 900000, output_tokens: 40000 };
  if (Object.keys(use).length) row.server_tool_use = use;
  return row;
}

/** One bucket of GET /v1/organizations/cost_report. */
function cost(costType = 'web_search', amount = '1174.40') {
  return {
    starting_at: '2026-08-01T00:00:00Z',
    results: [{ cost_type: costType, amount, currency: 'USD' }],
  };
}

test('the counter is nested and a flat read finds nothing', () => {
  const rows = fold([page(usage({ searches: 60000 }), usage({ searches: 58400 }))]);
  assert.equal(rows.apikey_01Rs.web_search, 118400);
  assert.equal(fold([page(usage())]).apikey_01Rs.web_search, 0);
});

test('the fee is per thousand searches, not per search', () => {
  assert.equal(fee(118400), 1184.00);
  assert.equal(fee(1), 0.01);
  assert.equal(fee(0), 0);
  assert.equal(fee(null), 0);
});

test('a high volume key is the finding and quotes the fee', () => {
  const [state, detail] = verdict({ web_search: 118400, other_tools: {} });
  assert.equal(state, 'search-fee');
  assert.match(detail, /tool fee of about \$1184\.00/);
});

test('a handful of searches is a demo and not a bill', () => {
  assert.equal(verdict({ web_search: 12, other_tools: {} })[0], 'low-volume');
  assert.equal(verdict({ web_search: 0, other_tools: {} })[0], 'no-searches');
  assert.equal(verdict({})[0], 'no-searches');
});

test('an unknown server tool counter stays visible', () => {
  const rows = fold([page(usage({
    searches: 200,
    tools: { web_fetch_requests: 90, code_execution_sessions: 0 },
  }))]);
  assert.equal(rows.apikey_01Rs.web_search, 200);
  assert.deepEqual(rows.apikey_01Rs.other_tools, { web_fetch_requests: 90 });
});

test('only the web_search cost type counts and amount is a string', () => {
  const buckets = [cost('web_search', '1174.40'), cost('web_search', '10.00'),
                   cost('code_execution', '500.00'), cost('web_search', '')];
  assert.equal(searchSpend(buckets), 1184.40);
  assert.equal(searchSpend([]), 0);
});

test('the four ways the two reports can disagree stay four answers', () => {
  assert.equal(reconcile(1184.00, 1174.40)[0], 'confirmed');
  assert.equal(reconcile(1184.00, 0)[0], 'unpriced');
  assert.equal(reconcile(0, 1174.40)[0], 'billed-without-count');
  const [state, detail] = reconcile(100.00, 900.00);
  assert.equal(state, 'mismatch');
  assert.match(detail, /800% apart/);
  assert.equal(reconcile(0, 0)[0], 'no-searches');
});
