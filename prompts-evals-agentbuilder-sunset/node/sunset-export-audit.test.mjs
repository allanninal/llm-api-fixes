import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AGENT_BUILDER, SHUTDOWN, daysLeft, exportCommand, exportPlan,
         promptIdState, repairLines, surfaceReach } from './sunset-export-audit.mjs';

const TODAY = '2026-08-31';

test('a surface with no api is never promoted by a stray status', () => {
  for (const status of [null, 200, 404, 401]) {
    const [state, detail] = surfaceReach(AGENT_BUILDER, status);
    assert.equal(state, 'no-api-surface');
    assert.ok(detail.includes('no documented REST endpoints'));
  }
  assert.ok(repairLines('no-api-surface').some((l) => l.includes('open Agent Builder')));
});

test('a 404 on the prompts path means no listing and not gone', () => {
  const [state, detail] = surfaceReach('prompts', 404);
  assert.equal(state, 'no-list-endpoint');
  assert.ok(detail.includes('your own call sites'));
  assert.ok(!detail.includes('gone'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('grep of your own tree')));
  assert.ok(lines.some((l) => l.includes('impossible before the export')));
});

test('the plan puts a person against what no script can reach', () => {
  const plan = exportPlan([['evals', 'enumerable'],
                           ['prompts', 'no-list-endpoint'],
                           [AGENT_BUILDER, 'no-api-surface'],
                           ['something', 'credentials']]);
  const owners = Object.fromEntries(plan.map(([n, o]) => [n, o]));
  assert.equal(owners.evals, 'a script');
  assert.equal(owners.prompts, 'a script, by id');
  assert.equal(owners[AGENT_BUILDER], 'a person');
  assert.ok(owners.something.startsWith('a person, until'));
  assert.equal(plan.length, 4);
});

test('an id that is not a prompt id is caught without a request', () => {
  const [state, detail] = promptIdState('promptx', null);
  assert.equal(state, 'not-a-prompt-id');
  assert.ok(detail.includes('start pmpt_'));
  assert.equal(promptIdState('', null)[0], 'malformed');
  assert.equal(promptIdState(null, 200)[0], 'malformed');
  assert.equal(promptIdState('pmpt_a1b2', null)[0], 'not-probed');
});

test('a declared id is graded by what answered for it', () => {
  assert.equal(promptIdState('pmpt_a1b2', 200)[0], 'readable');
  const [state, detail] = promptIdState('  pmpt_c3d4  ', 404);
  assert.equal(state, 'not-readable');
  assert.ok(detail.includes('out of the dashboard'));
  assert.equal(promptIdState('pmpt_c3d4', 401)[0], 'credentials');
  assert.equal(promptIdState('pmpt_c3d4', 500)[0], 'refused');
});

test('the export command is a read', () => {
  const line = exportCommand('evals');
  assert.ok(line.startsWith('curl -s '));
  assert.ok(line.includes('/v1/evals?limit=100'));
  assert.ok(line.includes('$OPENAI_API_KEY'));
  assert.ok(!line.includes('-X'));
  assert.ok(exportCommand('prompt', 'pmpt_a1b2').endsWith('export/pmpt_a1b2.json'));
  assert.equal(exportCommand('agent-builder'), '');
});

test('the date is the export deadline and the arithmetic says so', () => {
  assert.equal(daysLeft(TODAY), 91);
  assert.equal(daysLeft('2026-11-30'), 0);
  assert.equal(daysLeft('2026-12-05'), -5);
  assert.equal(SHUTDOWN, '2026-11-30');
});
