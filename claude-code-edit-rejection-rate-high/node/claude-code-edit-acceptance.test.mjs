import { test } from 'node:test';
import assert from 'node:assert/strict';
import { acceptance, actionsOf, actorName, dayStrings, fold, mask, repairLines,
         totals, verdict, worstTool }
  from './claude-code-edit-acceptance.mjs';

const record = (email, tools, commits = 0, cents = 0, prs = 0) => ({
  date: '2026-08-30',
  actor: { type: 'user_actor', email_address: email },
  core_metrics: { num_sessions: 6, commits_by_claude_code: commits,
                  pull_requests_by_claude_code: prs,
                  lines_of_code: { added: 400, removed: 90 } },
  tool_actions: tools,
  model_breakdown: [{ model: 'claude-opus-5', tokens: { input: 1, output: 1 },
                      estimated_cost: { currency: 'USD', amount: cents } }],
});

const page = (records) => ({ data: records, has_more: false });

test('a majority of generated diffs being thrown away is the finding', () => {
  const rows = fold([page([record('busy@example.com', {
    edit_tool: { accepted: 120, rejected: 80 },
    multi_edit_tool: { accepted: 36, rejected: 136 },
    write_tool: { accepted: 0, rejected: 40 },
  }, 4, 31040)])]);
  const row = rows['busy@example.com'];
  assert.deepEqual(totals(row), [156, 256]);

  const [state, detail] = verdict(row);
  assert.equal(state, 'rejected-more-than-kept');
  assert.match(detail, /38% accepted over 412 proposal/);
  assert.match(detail, /worst tool write_tool at 0%/);
  assert.equal(worstTool(row)[0], 'write_tool');
  assert.ok(repairLines(state, row).some((l) => l.includes('CLAUDE.md')));
});

test('a bad afternoon is not a pattern', () => {
  const rows = fold([page([record('quiet@example.com', {
    edit_tool: { accepted: 2, rejected: 7 } })])]);
  const [state, detail] = verdict(rows['quiet@example.com']);
  assert.equal(state, 'too-few-proposals');
  assert.match(detail, /under the floor of 20/);
  assert.deepEqual(repairLines(state, rows['quiet@example.com']), []);
});

test('the commits travel with the rate so it is never read alone', () => {
  const landing = fold([page([record('lands@example.com', {
    edit_tool: { accepted: 90, rejected: 160 } }, 26)])]);
  const row = landing['lands@example.com'];
  const [state] = verdict(row);
  assert.equal(state, 'rejected-more-than-kept');
  assert.ok(repairLines(state, row).some((l) => l.includes('26 commit(s)')));

  const empty = fold([page([record('none@example.com', {
    edit_tool: { accepted: 90, rejected: 160 } }, 0)])]);
  assert.ok(repairLines('rejected-more-than-kept', empty['none@example.com'])
    .some((l) => l.includes('no commits landed')));
});

test('an actor who proposed nothing has no rate rather than zero', () => {
  assert.equal(acceptance({ accepted: 0, rejected: 0 }), null);
  assert.equal(acceptance({}), null);
  assert.equal(acceptance({ accepted: 3, rejected: 1 }), 0.75);
  assert.equal(worstTool({ tools: {} }), null);
  assert.equal(worstTool({ tools: { edit_tool: { accepted: 1, rejected: 2 } } }), null);
});

test('a tool nobody used is absent and not a zero', () => {
  const actions = actionsOf({ tool_actions: {
    edit_tool: { accepted: 4, rejected: 1 },
    write_tool: { accepted: 0, rejected: 0 },
    bash_tool: { accepted: 99, rejected: 99 } } });
  assert.deepEqual(Object.keys(actions), ['edit_tool']);
  assert.deepEqual(actionsOf({}), {});
  assert.deepEqual(actionsOf(null), {});
  assert.deepEqual(actionsOf({ tool_actions: { edit_tool: { accepted: 'x', rejected: 3 } } }),
    { edit_tool: { accepted: 0, rejected: 3 } });
});

test('counts accumulate across days and across actor shapes', () => {
  const day = [
    record('a@example.com', { edit_tool: { accepted: 10, rejected: 5 } }, 1, 500),
    { actor: { type: 'api_actor', api_key_name: 'ci-runner' },
      core_metrics: { num_sessions: 1 },
      tool_actions: { edit_tool: { accepted: 30, rejected: 2 } },
      model_breakdown: [] },
  ];
  const rows = fold([page(day), page(day)]);
  assert.deepEqual(totals(rows['a@example.com']), [20, 10]);
  assert.equal(rows['a@example.com'].commits, 2);
  assert.equal(rows['a@example.com'].cents, 1000);
  assert.equal(rows['a@example.com'].added, 800);
  assert.deepEqual(totals(rows['ci-runner']), [60, 4]);
  assert.equal(verdict(rows['ci-runner'])[0], 'healthy');
});

test('actors are resolved and masked before being printed', () => {
  assert.equal(actorName({ actor: { email_address: 'a@example.com' } }),
               'a@example.com');
  assert.equal(actorName({ actor: { api_key_name: 'ci' } }), 'ci');
  assert.equal(actorName({}), 'unattributed');
  assert.equal(mask('someone@example.com'), 's***@example.com');
  assert.equal(mask('ci-runner'), 'ci-runner');
  assert.equal(mask(null), 'unattributed');
});

test('the thin band sits between kept and healthy', () => {
  assert.equal(verdict({ tools: { edit_tool: { accepted: 61, rejected: 39 } },
                         commits: 0 })[0], 'low-acceptance');
  assert.equal(verdict({ tools: { edit_tool: { accepted: 88, rejected: 12 } },
                         commits: 0 })[0], 'healthy');
  assert.deepEqual(dayStrings(2, new Date('2026-03-01T06:00:00Z')),
    ['2026-02-28', '2026-02-27']);
  assert.deepEqual(fold([]), {});
  assert.deepEqual(fold(null), {});
});
