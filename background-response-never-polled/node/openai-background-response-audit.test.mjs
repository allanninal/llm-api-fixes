import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ageOf, classify, errorCode, readIds, reasonFor, repairLines, summarise,
         verdict } from './openai-background-response-audit.mjs';

const NOW = 1800000000;
const SLA = 30 * 60;

function record(status, created = null, http = 200, extra = {}) {
  const body = { id: 'resp_x', status, ...extra };
  if (created !== null) body.created_at = created;
  return { http, response: body, created_hint: null };
}

test('each documented status gets its own bucket', () => {
  assert.equal(classify(record('completed', NOW - 60), NOW, SLA)[0], 'completed');
  assert.equal(classify(record('cancelled', NOW - 60), NOW, SLA)[0], 'cancelled');
  const incomplete = record('incomplete', NOW - 60, 200,
    { incomplete_details: { reason: 'max_output_tokens' } });
  const [b1, d1] = classify(incomplete, NOW, SLA);
  assert.equal(b1, 'incomplete');
  assert.ok(d1.includes('max_output_tokens'));
  const failed = record('failed', NOW - 60, 200,
    { error: { code: 'server_error', message: 'boom' } });
  const [b2, d2] = classify(failed, NOW, SLA);
  assert.equal(b2, 'failed');
  assert.ok(d2.includes('error.code server_error'));
  assert.equal(errorCode(failed.response), 'server_error');
  assert.equal(classify(record('weird', NOW), NOW, SLA)[0], 'unreadable');
  assert.equal(reasonFor({}), '');
});

test('queued is normal until the service level says it is not', () => {
  const running = record('in_progress', NOW - 4 * 60);
  const [b1, d1] = classify(running, NOW, SLA);
  assert.equal(b1, 'running');
  assert.ok(d1.includes('inside the service level'));
  const [b2, d2] = classify(running, NOW, 3 * 60);
  assert.equal(b2, 'stranded');
  assert.ok(d2.startsWith('in_progress for 4 min'));
  assert.equal(classify(record('queued', NOW - 19 * 3600), NOW, SLA)[0], 'stranded');
  const noStamp = { http: 200, response: { status: 'queued' }, created_hint: NOW - 7200 };
  assert.equal(classify(noStamp, NOW, SLA)[0], 'stranded');
  assert.equal(ageOf({}, null, NOW), null);
  assert.equal(classify({ http: 200, response: { status: 'queued' }, created_hint: null },
    NOW, SLA)[0], 'running');
});

test('a 404 means two different things and zdr decides which', () => {
  const lost = { http: 404, response: {}, created_hint: NOW - 86400 };
  assert.equal(classify(lost, NOW, SLA)[0], 'gone');
  const [b, d] = classify(lost, NOW, SLA, true);
  assert.equal(b, 'aged-out');
  assert.ok(d.includes('ten minutes'));
  const fresh = { http: 404, response: {}, created_hint: NOW - 60 };
  assert.equal(classify(fresh, NOW, SLA, true)[0], 'gone');
  assert.equal(classify({ http: 500, response: {} }, NOW, SLA)[0], 'unreadable');
});

test('the id file takes bare ids timestamps comments and duplicates', () => {
  const text = ['# open jobs', '', 'resp_a', 'resp_b,1799990000', 'resp_a',
    '  resp_c , not-a-number  '].join('\n');
  assert.deepEqual(readIds(text),
    [['resp_a', null], ['resp_b', 1799990000], ['resp_c', null]]);
  assert.deepEqual(readIds(''), []);
  assert.deepEqual(readIds(null), []);
});

test('an empty id list is a finding and not a clean run', () => {
  const [state, detail] = verdict([], SLA);
  assert.equal(state, 'background-no-ids');
  assert.ok(detail.includes('no list endpoint'));
  assert.ok(repairLines(state, []).some((l) => l.includes('transactionally')));
  const drained = [{ id: 'a', bucket: 'completed', code: '' }];
  assert.equal(verdict(drained, SLA)[0], 'background-drained');
  assert.deepEqual(summarise(drained), { completed: 1 });
});

test('transient and permanent error codes get different repairs', () => {
  const rows = [
    { id: 'a', bucket: 'stranded', code: '' },
    { id: 'b', bucket: 'failed', code: 'server_error' },
    { id: 'c', bucket: 'failed', code: 'invalid_prompt' },
    { id: 'd', bucket: 'gone', code: '' },
    { id: 'e', bucket: 'incomplete', code: '' },
  ];
  const [state, detail] = verdict(rows, SLA);
  assert.equal(state, 'background-stranded');
  assert.ok(detail.includes('2 failed') && detail.includes('no longer retrievable'));
  const lines = repairLines(state, rows);
  const retry = lines.filter((l) => l.startsWith('retry'));
  const escalate = lines.filter((l) => l.startsWith('escalate'));
  assert.ok(retry.length && retry[0].includes('server_error')
    && !retry[0].includes('invalid_prompt'));
  assert.ok(escalate.length && escalate[0].includes('invalid_prompt'));
  assert.ok(lines.some((l) => l.includes('background true can be cancelled')));
  assert.ok(lines.some((l) => l.includes('incomplete_details.reason')));
  assert.equal(summarise(rows).stranded, 1);
});
