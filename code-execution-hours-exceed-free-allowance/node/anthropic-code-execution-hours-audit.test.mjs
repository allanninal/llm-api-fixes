import { test } from 'node:test';
import assert from 'node:assert/strict';
import { amount, billedHours, codeExecutionSpend, executionsCeiling, fold,
         usageReportMentionsCodeExecution, verdict }
  from './anthropic-code-execution-hours-audit.mjs';

/** One bucket of GET /v1/organizations/cost_report. */
function cost({ workspace = 'wrkspc_01Qy', costType = 'code_execution',
                value = '84.60' } = {}) {
  return {
    starting_at: '2026-08-01T00:00:00Z',
    results: [{ workspace_id: workspace, cost_type: costType,
                description: 'Code Execution Usage', amount: value, currency: 'USD' }],
  };
}

/** One page of the messages usage report, as rich as it actually gets. */
function usagePage() {
  return {
    data: [{
      starting_at: '2026-08-01T00:00:00Z',
      results: [{
        uncached_input_tokens: 900000, output_tokens: 40000,
        cache_read_input_tokens: 120000,
        cache_creation: { ephemeral_5m_input_tokens: 30000, ephemeral_1h_input_tokens: 0 },
        server_tool_use: { web_search_requests: 200 },
        model: 'claude-sonnet-5', api_key_id: 'apikey_01Rs',
      }],
    }],
    has_more: false,
  };
}

test('any non-zero amount means the allowance is already gone', () => {
  const [state, detail] = verdict(0.60);
  assert.equal(state, 'allowance-just-crossed');
  assert.match(detail, /12 container hour\(s\) on top of the free 1550/);
  assert.equal(verdict(0)[0], 'within-allowance');
});

test('the states scale with how far past the allowance you are', () => {
  assert.equal(verdict(40.00)[0], 'allowance-spent');
  const [state, detail] = verdict(84.60);
  assert.equal(state, 'allowance-dwarfed');
  assert.match(detail, /1692 container hour/);
});

test('the usage report cannot see this line at all', () => {
  assert.equal(usageReportMentionsCodeExecution([usagePage()]), false);
  const future = { data: [{ results: [{ code_execution_container_hours: 12 }] }] };
  assert.equal(usageReportMentionsCodeExecution([future]), true);
});

test('amount is a decimal string and folds by workspace and type', () => {
  assert.equal(amount({ amount: '84.60' }), 84.60);
  assert.equal(amount({ amount: '' }), 0);
  assert.equal(amount({}), 0);
  const folded = fold([cost({ value: '80.00' }), cost({ value: '4.60' }),
                       cost({ costType: 'web_search', value: '500.00' }),
                       cost({ workspace: 'wrkspc_02Zz', costType: 'tokens', value: '9.00' })]);
  assert.equal(folded.wrkspc_01Qy.code_execution, 84.60);
  assert.equal(folded.wrkspc_01Qy.web_search, 500.00);
  assert.deepEqual(codeExecutionSpend(folded), { wrkspc_01Qy: 84.60 });
});

test('hours are derived from the published rate', () => {
  assert.equal(billedHours(84.60), 1692);
  assert.equal(billedHours(0), 0);
  assert.equal(billedHours(0.60), 12);
});

test('the execution figure is a ceiling and not a count', () => {
  assert.equal(executionsCeiling(12), 144);
  assert.equal(executionsCeiling(0), 0);
});
