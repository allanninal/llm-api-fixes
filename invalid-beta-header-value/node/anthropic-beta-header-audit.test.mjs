import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyProbe, conflicting, graduationVerdict, keySets, levenshtein,
         loadCallSites, nearMatches, repairLines, shapeDelta,
         splitBetas } from './anthropic-beta-header-audit.mjs';

const filesListing = (beta) => (beta
  ? { data: [{ id: 'file_01', type: 'file', size_bytes: 12 }],
      has_more: false, first_id: 'file_01', last_id: 'file_01' }
  : { data: [{ id: 'file_01', type: 'file', size_bytes: 12, expires_at: null }],
      next_page: null });

test('a misspelled beta is rejected and the suggestion is the repair', () => {
  const [state, detail] = classifyProbe('contxt-1m-2025-08-07', 400);
  assert.equal(state, 'rejected-typo');
  assert.ok(detail.includes('not entitled to'));
  const matches = nearMatches('contxt-1m-2025-08-07');
  assert.equal(matches[0], 'context-1m-2025-08-07');
  const lines = repairLines(state, 'contxt-1m-2025-08-07', matches);
  assert.ok(lines.some((l) => l.includes('context-1m-2025-08-07')));
  assert.ok(lines.some((l) => l.includes('entitlement')));
});

test('a graduated beta returns 200 and pins the older shape', () => {
  const deltas = { '/files': shapeDelta(filesListing(true), filesListing(false)) };
  const [state, detail] = graduationVerdict('files-api-2025-04-14', deltas);
  assert.equal(state, 'pinned-to-beta-shape');
  assert.ok(detail.includes('/files'));
  assert.deepEqual(deltas['/files'].top[0], ['first_id', 'has_more', 'last_id']);
  assert.deepEqual(deltas['/files'].top[1], ['next_page']);
  assert.deepEqual(deltas['/files'].item[1], ['expires_at']);
  const lines = repairLines(state, 'files-api-2025-04-14', [], deltas);
  assert.ok(lines.some((l) => l.includes('expires_at')));
  assert.ok(lines.some((l) => l.includes('graduated')));
});

test('identical bodies prove nothing and are not a finding', () => {
  const same = { data: [{ id: 'm1' }], has_more: false };
  const deltas = { '/models': shapeDelta(same, same) };
  const [state, detail] = graduationVerdict('context-management-2025-06-27', deltas);
  assert.equal(state, 'no-visible-difference');
  assert.ok(detail.includes('not evidence that the header does nothing'));
  assert.deepEqual(repairLines(state), []);
});

test('the published enum is a dictionary and not the verdict', () => {
  const [state, detail] = classifyProbe('brand-new-beta-2026-09-01', 200);
  assert.equal(state, 'accepted-undocumented');
  assert.ok(detail.includes('the list is behind'));
  assert.equal(classifyProbe('files-api-2025-04-14', 200)[0], 'accepted');
  assert.deepEqual(nearMatches('nothing-like-a-beta-name'), []);
  assert.equal(classifyProbe('nothing-like-a-beta-name', 400)[0], 'rejected-unknown');
  assert.equal(levenshtein('abc', 'abc'), 0);
  assert.equal(levenshtein('', 'abc'), 3);
});

test('the header is one string carrying a list so it can be malformed', () => {
  let [names, faults] = splitBetas('files-api-2025-04-14, skills-2025-10-02,');
  assert.deepEqual(names, ['files-api-2025-04-14', 'skills-2025-10-02']);
  assert.ok(faults.some((f) => f.includes('trailing comma')));

  [names, faults] = splitBetas('Skills-2025-10-02,skills-2025-10-02');
  assert.deepEqual(names, ['skills-2025-10-02']);
  assert.ok(faults.some((f) => f.includes('lower case')));
  assert.ok(faults.some((f) => f.includes('more than once')));

  [names, faults] = splitBetas('files api 2025-04-14');
  assert.ok([...names, ...faults].some((f) => f.includes('whitespace inside')));
  assert.ok(repairLines('malformed-header').some((l) => l.includes('comma separated')));
});

test('the documented conflicting pair needs no request at all', () => {
  assert.deepEqual(conflicting(['agent-memory-2026-07-22', 'managed-agents-2026-04-01']),
                   [['agent-memory-2026-07-22', 'managed-agents-2026-04-01']]);
  assert.deepEqual(conflicting(['managed-agents-2026-04-01']), []);
  assert.ok(repairLines('conflicting-pair').some((l) => l.includes('replaces the second')));
});

test('input and bodies are read in whatever shape they arrive', () => {
  assert.deepEqual(loadCallSites('{"a.py": "x,y"}'), { 'a.py': 'x,y' });
  assert.deepEqual(loadCallSites('["x", "y"]'), { '(declared)': 'x,y' });
  assert.deepEqual(loadCallSites('x,y'), { '(declared)': 'x,y' });
  assert.deepEqual(loadCallSites(''), {});
  assert.deepEqual(keySets(null), [[], []]);
  assert.deepEqual(keySets({ data: [] }), [['data'], []]);
  assert.equal(classifyProbe('files-api-2025-04-14', null)[0], 'unreachable');
  assert.equal(classifyProbe('files-api-2025-04-14', 401)[0], 'credentials');
});
