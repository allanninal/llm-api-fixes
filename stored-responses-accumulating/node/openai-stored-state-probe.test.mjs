import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RESPONSE_RETENTION_FLOOR_DAYS, ageDays, coverageNote, gradeConversation,
         gradeResponse, itemTotals, parseRecords, repairLines,
         responseRow } from './openai-stored-state-probe.mjs';

const NOW = 1_800_000_000;
const DAY = 86400;

const resp = (id, daysOld, conversation = null) => responseRow({
  id, object: 'response', status: 'completed',
  created_at: NOW - Math.trunc(daysOld * DAY),
  conversation: conversation ? { id: conversation } : null,
  metadata: { tenant: 'acme' },
});

const items = (n, newestDaysOld = 1) => Array.from({ length: n }, (_, i) => ({
  id: `msg_${i}`, type: 'message',
  created_at: NOW - Math.trunc((newestDaysOld + n - i - 1) * DAY),
}));

test('retention is read as a floor and a 404 keeps both its causes', () => {
  const [state, detail] = gradeResponse(resp('resp_a19', 94.2), 200, NOW, 30);
  assert.equal(state, 'retained-past-policy');
  assert.ok(detail.includes('still readable 94.2 day(s)'));
  assert.ok(detail.includes('past your 30 day policy'));
  assert.ok(detail.includes(`at least ${RESPONSE_RETENTION_FLOOR_DAYS} days`));
  assert.ok(detail.includes('a floor and not a deadline'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('DELETE /v1/responses/{response_id}')));
  assert.ok(lines.some((l) => l.includes('id ledger')));
  const [gone, goneDetail] = gradeResponse(null, 404, NOW, 30);
  assert.equal(gone, 'not-retained');
  assert.ok(goneDetail.includes('store false') && goneDetail.includes('aged out'));
  assert.deepEqual(repairLines(gone), []);
  assert.equal(gradeResponse(resp('resp_z1', 1), 403, NOW, 30)[0], 'probe-unreadable');
});

test('the row has no chain in it and no store flag', () => {
  const row = responseRow({ id: 'resp_b40', created_at: 1700000000,
                            previous_response_id: 'resp_a01', status: 'completed',
                            conversation: { id: 'conv_x1' },
                            metadata: { tenant: 'acme', env: 'prod' } });
  assert.deepEqual(row, { id: 'resp_b40', created_at: 1700000000,
                          status: 'completed', conversation: 'conv_x1',
                          metadata_keys: 2 });
  assert.ok(!('previous_response_id' in row));
  assert.ok(!('store' in row) && !('stored' in row));
  assert.equal(responseRow(null).id, '');
  assert.equal(responseRow({ created_at: 'nonsense' }).created_at, 0);
  assert.equal(responseRow({ metadata: 'nope' }).metadata_keys, 0);
});

test('deleting the conversation does not delete its items', () => {
  const [state, detail] = gradeResponse(resp('resp_b40', 4.1, 'conv_x1'), 200, NOW, 30);
  assert.equal(state, 'items-outlive-response');
  assert.ok(detail.includes('inside your policy'));
  assert.ok(detail.includes('conv_x1') && detail.includes('retained until deleted'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('not enough here')));
  assert.ok(lines.some((l) => l.includes('items/{item_id}')));
  assert.ok(lines.some((l) => l.includes('does not delete its items')));
  const over = gradeResponse(resp('resp_c11', 91, 'conv_x1'), 200, NOW, 30);
  assert.equal(over[0], 'retained-past-policy');
  assert.ok(over[1].includes('retained until deleted'));
});

test('volume and idleness are two findings on one object', () => {
  const busy = itemTotals(items(4182, 0.5));
  assert.equal(busy.count, 4182);
  assert.ok(busy.newest > busy.oldest);
  const [state, detail] = gradeConversation({ id: 'conv_x1' }, busy, 200, NOW, 30, 500);
  assert.equal(state, 'thread-unbounded');
  assert.ok(detail.includes('4182 item(s) and no TTL'));
  assert.ok(repairLines(state).some((l) => l.includes('seeded with a summary')));
  const idle = itemTotals(items(12, 211.4));
  const [idleState, idleDetail] = gradeConversation({ id: 'conv_y7' }, idle, 200,
                                                    NOW, 30, 500);
  assert.equal(idleState, 'thread-idle');
  assert.ok(idleDetail.includes('211.4 day(s) ago'));
  assert.ok(idleDetail.includes('retained until deleted'));
  const fine = itemTotals(items(12, 1));
  assert.equal(gradeConversation({}, fine, 200, NOW, 30, 500)[0], 'thread-within-policy');
  assert.equal(gradeConversation(null, null, 404, NOW, 30, 500)[0], 'not-retained');
});

test('ids are routed by prefix and what cannot be routed is kept', () => {
  const records = parseRecords('resp_a19\n\n# exported 2026-08-31\nconv_x1\n'
    + 'resp_a19\nlegacy-7742  # old schema\n   \nconv_y7\n');
  assert.deepEqual(records.responses, ['resp_a19']);
  assert.deepEqual(records.conversations, ['conv_x1', 'conv_y7']);
  assert.deepEqual(records.unrecognised, ['legacy-7742']);
  assert.deepEqual(parseRecords(''), { responses: [], conversations: [], unrecognised: [] });
  assert.ok(repairLines('unrecognised-id').some((l) => l.includes('hole in a coverage figure')));
});

test('the coverage sentence is printed whatever the run found', () => {
  const note = coverageNote({ responses: Array(388).fill('resp_a19'),
                              conversations: Array(22).fill('conv_x1'),
                              unrecognised: ['x', 'y'] });
  assert.ok(note.includes('412 id(s) supplied'));
  assert.ok(note.includes('388 response(s), 22 conversation(s), 2 unroutable'));
  assert.ok(note.includes('has a list endpoint'));
  assert.ok(note.includes('your records and not your account'));
  assert.ok(coverageNote({}).includes('has a list endpoint'));
  assert.ok(coverageNote(null).includes('0 id(s) supplied'));
  assert.equal(ageDays(0, NOW), null);
  assert.equal(ageDays('x', NOW), null);
  assert.deepEqual(itemTotals(null), { count: 0, oldest: 0, newest: 0 });
});
