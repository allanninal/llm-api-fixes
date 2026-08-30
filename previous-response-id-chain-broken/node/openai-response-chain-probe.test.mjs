import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RETENTION_DAYS, ageDays, classifyChain, linkRow, oldestLink, parseIds,
         repairLines, runwayDays } from './openai-response-chain-probe.mjs';

const NOW = 1_800_000_000;
const DAY = 86400;

const link = (id, daysOld, parent = '', conversation = '') => linkRow({
  id,
  created_at: NOW - Math.trunc(daysOld * DAY),
  previous_response_id: parent,
  conversation: conversation ? { id: conversation } : null,
  status: 'completed',
});

test('a missing parent is the finding and names the next turn', () => {
  const chain = [link('resp_c9', 1, 'resp_a1')];
  const [state, detail] = classifyChain('resp_c9', chain, 'resp_a1', '', false, NOW, 5);
  assert.equal(state, 'chain-broken');
  assert.ok(detail.includes('resp_a1 no longer resolves'));
  assert.ok(detail.includes('next turn on this thread will 404'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('replaying local history')));
  assert.ok(lines.some((l) => l.includes('no 30 day TTL')));

  const [headState, headDetail] = classifyChain('resp_c9', [], 'resp_c9', '', false, NOW, 5);
  assert.equal(headState, 'chain-broken');
  assert.ok(headDetail.includes('aged out') && headDetail.includes('never stored'));
});

test('the runway comes from the oldest link and not the newest', () => {
  const chain = [link('resp_f2', 0.5, 'resp_e1'),
                 link('resp_e1', 12, 'resp_d7'),
                 link('resp_d7', 26.4)];
  assert.equal(oldestLink(chain).id, 'resp_d7');
  assert.ok(Math.abs(runwayDays(chain, NOW) - (RETENTION_DAYS - 26.4)) < 0.01);
  const [state, detail] = classifyChain('resp_f2', chain, '', '', false, NOW, 5);
  assert.equal(state, 'chain-expiring');
  assert.ok(detail.includes('26.4 days old') && detail.includes('3.6 days'));
  assert.ok(ageDays(chain[0].created_at, NOW) < 1);
});

test('a conversation backed chain is not this note', () => {
  const chain = [link('resp_k4', 1, 'resp_k3', 'conv_x1'),
                 link('resp_k3', 44, '', 'conv_x1')];
  const [state, detail] = classifyChain('resp_k4', chain, '', '', false, NOW, 5);
  assert.equal(state, 'conversation-backed');
  assert.ok(detail.includes('no 30 day TTL'));
  assert.deepEqual(repairLines(state), []);
  const mixed = [chain[0], link('resp_k3', 44)];
  assert.equal(classifyChain('resp_k4', mixed, '', '', false, NOW, 5)[0], 'chain-broken');
});

test('a chain cut short by the hop limit is not graded healthy', () => {
  const chain = [link('resp_z9', 1, 'resp_z8'), link('resp_z8', 2, 'resp_z7')];
  const [state, detail] = classifyChain('resp_z9', chain, '', '', true, NOW, 5);
  assert.equal(state, 'chain-unfinished');
  assert.ok(detail.includes('oldest link was never seen'));
  assert.ok(repairLines(state).some((l) => l.includes('--max-hops')));
  const rooted = [chain[0], link('resp_z8', 2)];
  assert.equal(classifyChain('resp_z9', rooted, '', '', false, NOW, 5)[0], 'chain-intact');
});

test('linkRow reads four fields and invents no store flag', () => {
  const row = linkRow({ id: 'resp_a1', created_at: 1700000000,
                        previous_response_id: null,
                        conversation: { id: 'conv_x1' }, status: 'completed' });
  assert.deepEqual(row, { id: 'resp_a1', created_at: 1700000000,
                          previous_response_id: '', conversation: 'conv_x1',
                          status: 'completed' });
  assert.ok(!('store' in row) && !('stored' in row));
  assert.equal(linkRow(null).id, '');
  assert.equal(linkRow({ created_at: 'nonsense' }).created_at, 0);
  assert.equal(ageDays(0, NOW), null);
  assert.equal(runwayDays([], NOW), null);
});

test('the id file is read the way it is actually exported', () => {
  const ids = parseIds('resp_a1\n\n# heads exported 2026-08-30\nresp_b2  # oldest\n'
    + 'resp_a1\n   \nresp_c3\n');
  assert.deepEqual(ids, ['resp_a1', 'resp_b2', 'resp_c3']);
  assert.deepEqual(parseIds(''), []);
  assert.deepEqual(parseIds(null), []);
  const [state, detail] = classifyChain('resp_a1', [], '', 'HTTP 403 reading resp_a1',
                                        false, NOW, 5);
  assert.equal(state, 'chain-unreadable');
  assert.ok(detail.includes('nothing about this chain was established'));
});
