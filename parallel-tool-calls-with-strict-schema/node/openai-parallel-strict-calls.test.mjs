import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, duplicateNames, exposure, functionCalls, parallelAllowed,
         parseIds, repairLines, strictTools, unvalidatedCalls }
  from './openai-parallel-strict-calls.mjs';

const STRICT_TOOLS = [
  { type: 'function', name: 'lookup_order', strict: true },
  { type: 'function', name: 'create_ticket', strict: true },
];

const turn = (calls, tools, parallel) => {
  const body = {
    tools: tools ?? STRICT_TOOLS,
    output: calls.map((name, i) => ({ type: 'function_call', name,
                                      call_id: `call_${i}` })),
  };
  if (parallel !== undefined) body.parallel_tool_calls = parallel;
  return body;
};

test('a turn that fans out under strict schemas has no guarantee', () => {
  const body = turn(['lookup_order', 'create_ticket', 'create_ticket']);
  assert.equal(parallelAllowed(body), true);
  assert.deepEqual(strictTools(body), ['create_ticket', 'lookup_order']);
  assert.equal(functionCalls(body).length, 3);

  const [state, detail] = classify(body);
  assert.equal(state, 'strict-void');
  assert.match(detail, /3 function_call item/);
  assert.match(detail, /carry no schema guarantee/);
  assert.match(repairLines(state)[0], /parallel_tool_calls false/);
});

test('the same configuration returning one call is at risk not clean', () => {
  const [state, detail] = classify(turn(['lookup_order']));
  assert.equal(state, 'strict-at-risk');
  assert.match(detail, /did not fire here/);

  const states = [
    ...Array.from({ length: 12 }, () => 'strict-void'),
    ...Array.from({ length: 988 }, () => 'strict-at-risk'),
    ...Array.from({ length: 400 }, () => 'no-strict-declared'),
  ];
  const shape = exposure(states);
  assert.equal(shape.atRisk, 1000);
  assert.equal(shape.void, 12);
  assert.equal(Number(shape.rate.toFixed(4)), 0.012);

  const rows = [
    ...Array.from({ length: 9 }, () => ({ state: 'strict-void', calls: 3 })),
    ...Array.from({ length: 988 }, () => ({ state: 'strict-at-risk', calls: 1 })),
  ];
  assert.equal(unvalidatedCalls(rows), 27);
});

test('turning parallel calls off restores the guarantee', () => {
  const [state, detail] = classify(turn(['lookup_order'], undefined, false));
  assert.equal(state, 'strict-serialised');
  assert.match(detail, /The guarantee holds/);
  assert.equal(parallelAllowed({ parallel_tool_calls: false }), false);
  assert.equal(parallelAllowed({ parallel_tool_calls: true }), true);
  assert.equal(parallelAllowed({}), true);
  assert.equal(exposure(Array.from({ length: 40 }, () => 'strict-serialised')).rate,
               null);
});

test('the same tool called twice keeps both call ids', () => {
  const calls = functionCalls(turn(['create_ticket', 'create_ticket']));
  assert.deepEqual(duplicateNames(calls), { create_ticket: 2 });
  assert.deepEqual(calls.map((c) => c.callId), ['call_0', 'call_1']);
  assert.deepEqual(duplicateNames([{ name: 'a' }, { name: 'b' }]), {});
  assert.deepEqual(duplicateNames(null), {});
});

test('a fan out with no strict tools is a different fault', () => {
  const loose = [{ type: 'function', name: 'lookup_order' },
                 { type: 'function', name: 'create_ticket', strict: false }];
  const [state, detail] = classify(turn(['lookup_order', 'create_ticket'], loose));
  assert.equal(state, 'fanout-no-strict');
  assert.match(detail, /no tool declares strict/);
  assert.match(detail, /different fault/);
  assert.match(repairLines(state)[0], /Validate tool arguments/);
  assert.equal(classify(turn([], loose))[0], 'no-strict-declared');
  assert.deepEqual(strictTools({ tools: loose }), []);
});

test('strict is read in both tool shapes', () => {
  const nested = [{ type: 'function',
                    function: { name: 'run_refund', strict: true } }];
  assert.deepEqual(strictTools({ tools: nested }), ['run_refund']);
  const [state] = classify({
    tools: nested,
    output: [{ type: 'function_call', name: 'run_refund', call_id: 'c1' },
             { type: 'function_call', name: 'run_refund', call_id: 'c2' }],
  });
  assert.equal(state, 'strict-void');
});

test('turns without tools and junk do not become findings', () => {
  assert.equal(classify({})[0], 'no-tools');
  assert.equal(classify(null)[0], 'no-tools');
  assert.equal(classify({ tools: [], output: [] })[0], 'no-tools');
  const body = turn([]);
  body.output = [{ type: 'message', content: [] }, null, 'nonsense'];
  assert.deepEqual(functionCalls(body), []);
  assert.equal(classify(body)[0], 'strict-at-risk');
  assert.equal(unvalidatedCalls(null), 0);
});

test('response ids are validated before they reach a url', () => {
  const text = 'resp_abc123\n# note\n\nresp_abc123\nresp_def456\n../../etc\n';
  assert.deepEqual(parseIds(text), ['resp_abc123', 'resp_def456']);
  assert.deepEqual(parseIds('resp_bad/../x'), []);
  assert.deepEqual(parseIds(null), []);
});
