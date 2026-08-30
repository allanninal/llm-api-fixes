import { test } from 'node:test';
import assert from 'node:assert/strict';
import { choiceKind, classify, countable, deferCandidates, fixedOverhead,
         monthlyCost, overhead, overheadShare, systemPromptTokens, toolNames,
         windowShare, withoutTool, withoutTools }
  from './anthropic-tool-schema-overhead.mjs';

const BODY = {
  model: 'claude-opus-5',
  max_tokens: 1024,
  temperature: 0,
  system: 'You are a support agent.',
  tool_choice: { type: 'auto' },
  messages: [{ role: 'user', content: 'where is my order' }],
  tools: [
    { name: 'search_knowledge_base', input_schema: { type: 'object' } },
    { name: 'create_ticket', input_schema: { type: 'object' } },
    { name: 'lookup_order', input_schema: { type: 'object' } },
  ],
};

test('the tools block is most of what you pay for', () => {
  const total = 12388;
  const base = 888;
  assert.equal(overhead(total, base), 11500);
  assert.equal(Number(overheadShare(total, base).toFixed(4)), 0.9283);

  const [state, detail] = classify(total, base);
  assert.equal(state, 'schema-dominates');
  assert.match(detail, /11500 of 12388 input token/);
  assert.match(detail, /888 token\(s\) of system and messages, a ratio of 13.0 to 1/);

  assert.equal(monthlyCost(11500, 10000, 3.0), 10350);
});

test('the ablation deltas do not add up to the whole', () => {
  const perTool = [{ name: 'search_knowledge_base', tokens: 6200 },
                   { name: 'create_ticket', tokens: 3100 },
                   { name: 'lookup_order', tokens: 1914 }];
  const [residual, measured] = fixedOverhead(11500, perTool);
  assert.equal(measured, 11214);
  assert.equal(residual, 286);
  assert.equal(residual, systemPromptTokens('claude-opus-5', 'auto'));
  assert.deepEqual(fixedOverhead(0, perTool), [0, 11214]);
});

test('the system prompt table matches on longest prefix', () => {
  assert.equal(systemPromptTokens('claude-opus-5'), 286);
  assert.equal(systemPromptTokens('claude-opus-5', 'any'), 406);
  assert.equal(systemPromptTokens('claude-sonnet-5'), 354);
  assert.equal(systemPromptTokens('claude-opus-4-5'), 496);
  assert.equal(systemPromptTokens('claude-haiku-4-5-20251001'), 496);
  assert.equal(systemPromptTokens('claude-opus-4-7', 'any'), 804);
  assert.equal(systemPromptTokens('claude-fable-5'), null);
  assert.equal(systemPromptTokens(''), null);
  assert.equal(systemPromptTokens(null), null);
});

test('removing the tools removes the tool_choice with them', () => {
  const stripped = withoutTools(BODY);
  assert.equal('tools' in stripped, false);
  assert.equal('tool_choice' in stripped, false);
  assert.equal(stripped.system, BODY.system);
  assert.deepEqual(stripped.messages, BODY.messages);
  assert.equal(BODY.tools.length, 3);
  assert.equal('tool_choice' in BODY, true);

  const oneOut = withoutTool(BODY, 'create_ticket');
  assert.deepEqual(toolNames(oneOut), ['search_knowledge_base', 'lookup_order']);
  assert.deepEqual(oneOut.tool_choice, BODY.tool_choice);

  const bare = withoutTool({ tools: [{ name: 'only' }], tool_choice: 'any' }, 'only');
  assert.equal('tools' in bare, false);
  assert.equal('tool_choice' in bare, false);
});

test('the deferral picker can never return every tool', () => {
  const rows = [{ name: 'a', tokens: 900 }, { name: 'b', tokens: 400 },
                { name: 'c', tokens: 100 }];
  const picked = deferCandidates(rows);
  assert.deepEqual(picked, ['b', 'c']);
  assert.ok(picked.length < rows.length);
  assert.deepEqual(deferCandidates(rows, ['a', 'b', 'c']), []);
  assert.deepEqual(deferCandidates(rows, ['a']), ['b', 'c']);
  assert.deepEqual(deferCandidates([{ name: 'only', tokens: 10 }]), []);
  assert.deepEqual(deferCandidates([]), []);
});

test('the counting body keeps what is being measured', () => {
  const body = countable(BODY);
  assert.equal('max_tokens' in body, false);
  assert.equal('temperature' in body, false);
  assert.deepEqual(body.tools, BODY.tools);
  assert.equal(body.model, 'claude-opus-5');
  assert.deepEqual(countable(null), {});
  assert.equal(choiceKind(BODY), 'auto');
  assert.equal(choiceKind({ tool_choice: { type: 'tool', name: 'x' } }), 'any');
  assert.equal(choiceKind({ tool_choice: 'any' }), 'any');
  assert.equal(choiceKind({}), 'auto');
});

test('the states are bounded and a missing number stays missing', () => {
  assert.equal(classify(1000, 900)[0], 'schema-modest');
  assert.equal(classify(1000, 700)[0], 'schema-heavy');
  assert.equal(classify(1000, 500)[0], 'schema-dominates');
  assert.equal(classify(1000, 1000)[0], 'no-tools');
  assert.equal(classify(0, 0)[0], 'nothing-counted');
  assert.equal(overheadShare(0, 0), null);
  assert.equal(overhead(500, 900), 0);
  assert.equal(monthlyCost(11500, 0, 3.0), null);
  assert.equal(monthlyCost(11500, 10, 'free'), null);
  assert.equal(windowShare(12388, 200000), 0.06194);
  assert.equal(windowShare(12388, 0), null);
});
