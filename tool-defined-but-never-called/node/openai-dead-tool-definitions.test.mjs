import { test } from 'node:test';
import assert from 'node:assert/strict';
import { choiceMode, classify, coverage, crowding, deadWeight, declaredTools,
         fold, orphanCalls, parseIds, toolName }
  from './openai-dead-tool-definitions.mjs';

const TOOLS = [
  { type: 'function', name: 'lookup_order', description: 'x'.repeat(200) },
  { type: 'function', name: 'cancel_order', description: 'x'.repeat(200) },
  { type: 'function', name: 'lookup_invoice', description: 'x'.repeat(200) },
  { type: 'function', name: 'escalate_to_human', description: 'x'.repeat(1000) },
];

const turn = (calls, choice, tools) => {
  const body = {
    tools: tools ?? TOOLS,
    output: calls.map((name) => ({ type: 'function_call', name, call_id: 'call_1' })),
  };
  if (choice !== undefined) body.tool_choice = choice;
  return body;
};

const byName = (corpus) =>
  Object.fromEntries(coverage(corpus).map((r) => [r.name, r]));

test('a tool declared on every turn and never chosen is dead weight', () => {
  const sample = [
    ...Array.from({ length: 300 }, () => turn(['lookup_order'])),
    ...Array.from({ length: 98 }, () => turn(['cancel_order'])),
    ...Array.from({ length: 2 }, () => turn(['lookup_invoice'])),
  ];
  const corpus = fold(sample);
  assert.equal(corpus.sampled, 400);
  assert.equal(corpus.withTools, 400);

  const rows = byName(corpus);
  assert.equal(rows.escalate_to_human.offered, 400);
  assert.equal(rows.escalate_to_human.calls, 0);

  const [state, detail] = classify(rows.escalate_to_human);
  assert.equal(state, 'never-called');
  assert.match(detail, /offered in 400 of 400 turn/);
  assert.equal(classify(rows.lookup_order)[0], 'called');
  assert.equal(classify(rows.lookup_invoice)[0], 'rarely-called');
});

test('a tool tool_choice never offered is a different finding', () => {
  const sample = Array.from({ length: 400 },
    () => turn(['lookup_order'], { type: 'function', name: 'lookup_order' }));
  const rows = byName(fold(sample));
  const [state, detail] = classify(rows.escalate_to_human);
  assert.equal(state, 'never-offered');
  assert.match(detail, /free to be chosen in 0 of them/);
  assert.equal(rows.lookup_order.offered, 400);
  assert.equal(classify(rows.lookup_order)[0], 'called');
});

test('tool_choice none is not evidence about anything', () => {
  const rows = byName(fold(Array.from({ length: 400 }, () => turn([], 'none'))));
  assert.equal(rows.lookup_order.turns, 400);
  assert.equal(rows.lookup_order.offered, 0);
  assert.equal(classify(rows.lookup_order)[0], 'never-offered');
  assert.equal(choiceMode({ tool_choice: 'none' }), 'blocked');
  assert.equal(choiceMode({}), 'free');
  assert.equal(choiceMode({ tool_choice: 'auto' }), 'free');
  assert.equal(choiceMode({ tool_choice: 'required' }), 'free');
});

test('both tool shapes are read', () => {
  const nested = [{ type: 'function', function: { name: 'run_refund' } }];
  assert.equal(toolName(nested[0]), 'run_refund');
  assert.equal(toolName({ type: 'function', name: 'flat' }), 'flat');
  assert.equal(toolName({ type: 'web_search' }), null);
  assert.equal(toolName(null), null);
  assert.deepEqual(declaredTools({ tools: [{ type: 'web_search' }] }), {});
  assert.deepEqual(Object.keys(declaredTools({ tools: nested })), ['run_refund']);
});

test('a small sample is not a verdict', () => {
  const rows = byName(fold(Array.from({ length: 11 }, () => turn([]))));
  const [state, detail] = classify(rows.lookup_order);
  assert.equal(state, 'too-small-a-sample');
  assert.match(detail, /under the floor of 50/);
  assert.equal(classify(rows.lookup_order, 5)[0], 'never-called');
});

test('the dead weight share is characters and stays characters', () => {
  const sample = Array.from({ length: 400 },
    () => turn(['lookup_order', 'cancel_order', 'lookup_invoice']));
  const share = deadWeight(coverage(fold(sample)));
  assert.ok(share > 0.5 && share < 0.75);
  assert.equal(deadWeight([]), null);
  assert.equal(deadWeight([{ name: 'a', chars: 0, turns: 1, offered: 1, calls: 0 }]),
               null);
});

test('a crowded turn is its own finding', () => {
  const wide = Array.from({ length: 26 },
    (_, i) => ({ type: 'function', name: `tool_${i}` }));
  const corpus = fold(Array.from({ length: 60 }, () => turn([], undefined, wide)));
  const [state, detail] = crowding(corpus.widestTurn);
  assert.equal(state, 'crowded');
  assert.match(detail, /offered 26 tools/);
  assert.equal(crowding(20)[0], 'within-guidance');
  assert.equal(crowding(0)[0], 'no-tools');
});

test('a mixed sample is reported rather than silently subtracted', () => {
  assert.deepEqual(orphanCalls(fold([turn(['from_another_config'])])),
                   ['from_another_config']);
  assert.deepEqual(orphanCalls(fold([turn(['lookup_order'])])), []);
  assert.deepEqual(fold([]), fold(null));
  assert.deepEqual(coverage(fold(null)), []);
});

test('response ids are validated before they reach a url', () => {
  const text = 'resp_abc123\n# a comment\n\nresp_abc123\nresp_def456\n../../etc\n';
  assert.deepEqual(parseIds(text), ['resp_abc123', 'resp_def456']);
  assert.deepEqual(parseIds('resp_bad/../x'), []);
  assert.deepEqual(parseIds(null), []);
});
