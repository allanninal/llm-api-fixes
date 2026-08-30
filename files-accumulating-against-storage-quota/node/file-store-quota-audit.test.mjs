import { test } from 'node:test';
import assert from 'node:assert/strict';
import { byPurpose, epoch, fileRow, gradeConcentration, gradeExpiry, gradeOutliers,
         gradeTotal, human, repairLines, totals } from './file-store-quota-audit.mjs';

const NOW = 1_800_000_000;
const DAY = 86400;
const GIB = 1024 ** 3;

const oai = (id, size, purpose = 'batch', daysOld = 1, expires = null) => fileRow({
  id, bytes: size, purpose, filename: `${id}.jsonl`,
  created_at: NOW - Math.trunc(daysOld * DAY), expires_at: expires,
}, 'openai');

test('the ceiling is an argument because no endpoint reports it', () => {
  const used = 90 * GIB;
  const [tight, detail] = gradeTotal(used, 100 * GIB);
  assert.equal(tight, 'quota-critical');
  assert.ok(detail.includes('90.0%') && detail.includes('headroom'));
  assert.equal(gradeTotal(used, 1000 * GIB)[0], 'quota-headroom');
  assert.equal(gradeTotal(used, 140 * GIB)[0], 'quota-warning');
  const [state, why] = gradeTotal(used, 0);
  assert.equal(state, 'quota-unknown');
  assert.ok(why.includes('without a denominator'));
  assert.ok(repairLines(state).some((l) => l.includes('--quota-bytes')));
});

test('two providers normalise to one shape and one clock', () => {
  const a = fileRow({ id: 'file-a1', bytes: 2048, purpose: 'batch_output',
                      created_at: 1700000000, expires_at: null }, 'openai');
  const b = fileRow({ id: 'file_b2', size_bytes: 2048, filename: 'doc.pdf',
                      created_at: '2023-11-14T22:13:20Z', expires_at: null },
                    'anthropic');
  assert.equal(a.size, 2048);
  assert.equal(b.size, 2048);
  assert.equal(a.purpose, 'batch_output');
  assert.equal(b.purpose, 'unclassified');
  assert.equal(a.created_at, 1700000000);
  assert.equal(b.created_at, 1700000000);
  assert.equal(epoch('2023-11-14T22:13:20+00:00'), 1700000000);
  assert.equal(epoch('2023-11-14T23:13:20+01:00'), 1700000000);
  assert.equal(epoch(null), 0);
  assert.equal(epoch(''), 0);
  assert.equal(epoch('last tuesday'), 0);
  assert.equal(a.expires_at, null);
  assert.equal(a.expiry_reported, true);
  assert.equal(fileRow({ id: 'file-c3', bytes: 1 }, 'openai').expiry_reported, false);
  assert.equal(fileRow(null, 'openai').size, 0);
  assert.equal(fileRow({ bytes: 'nonsense' }, 'openai').size, 0);
  assert.equal(human(2048), '2.0 KiB');
  assert.equal(human(0), '0 B');
  assert.equal(human(null), '0 B');
});

test('concentration only fires when one class really dominates', () => {
  const lopsided = [oai('file-1', 90 * GIB, 'batch_output'),
                    oai('file-2', 5 * GIB, 'fine-tune'),
                    oai('file-3', 5 * GIB, 'user_data')];
  const tot = totals(lopsided);
  assert.deepEqual(tot, { count: 3, bytes: 100 * GIB });
  const ranked = byPurpose(lopsided);
  assert.equal(ranked[0][0], 'batch_output');
  assert.equal(ranked[0][1], 1);
  const [state, detail] = gradeConcentration(ranked, tot.bytes);
  assert.equal(state, 'purpose-dominates');
  assert.ok(detail.includes('batch_output is 90.0%'));
  assert.ok(repairLines(state).some((l) => l.includes('DELETE /v1/files/{file_id}')));
  const even = [oai('file-4', 10 * GIB, 'batch'), oai('file-5', 10 * GIB, 'fine-tune'),
                oai('file-6', 10 * GIB, 'user_data')];
  const [flat, flatDetail] = gradeConcentration(byPurpose(even), totals(even).bytes);
  assert.equal(flat, 'purpose-even');
  assert.ok(flatDetail.includes('largest is'));
  assert.equal(gradeConcentration([], 0)[0], 'purpose-even');
});

test('the per file cap is a second ceiling and not a share of the first', () => {
  const rows = [oai('file-9f1', 487000000, 'fine-tune'), oai('file-a2', 1024)];
  assert.equal(gradeTotal(totals(rows).bytes, 16 * 1024 * GIB)[0], 'quota-headroom');
  const [state, detail, big] = gradeOutliers(rows, 512000000);
  assert.equal(state, 'file-near-cap');
  assert.ok(detail.includes('1 file(s)') && detail.includes('80%'));
  assert.deepEqual(big.map((r) => r.id), ['file-9f1']);
  assert.ok(repairLines(state).some((l) => l.includes('second ceiling')));
  assert.equal(gradeOutliers(rows, 16 * GIB)[0], 'file-sizes-fine');
  assert.equal(gradeOutliers(rows, 0)[0], 'cap-unknown');
});

test('expiry is the only grader that describes the future', () => {
  const stale = [oai('file-1', GIB, 'batch', 200), oai('file-2', GIB, 'batch', 200),
                 oai('file-3', GIB, 'batch', 2)];
  const [state, detail] = gradeExpiry(stale, NOW, 90);
  assert.equal(state, 'no-expiry-policy');
  assert.ok(detail.includes('3 of 3 file(s) have no expires_at'));
  assert.ok(detail.includes('2 of those are older than 90 day(s)'));
  assert.ok(repairLines(state).some((l) => l.includes('expires_in_seconds')));
  const covered = [oai('file-4', GIB, 'batch', 1, NOW + 10 * DAY)];
  const [clean, cleanDetail] = gradeExpiry(covered, NOW, 90);
  assert.equal(clean, 'expiry-covered');
  assert.ok(cleanDetail.includes('lifecycle'));
  assert.deepEqual(repairLines(clean), []);
  assert.equal(gradeExpiry([], NOW, 90)[0], 'expiry-none');
});

test('every repair is printed and none of them reclaims anything', () => {
  for (const state of ['quota-critical', 'quota-warning', 'purpose-dominates',
                       'file-near-cap', 'no-expiry-policy', 'quota-unknown']) {
    const lines = repairLines(state);
    assert.ok(lines.length, state);
    assert.ok(lines.every((l) => typeof l === 'string' && l.length));
  }
  assert.ok(repairLines('purpose-dominates').some((l) => l.includes('cannot be recovered')));
  assert.deepEqual(repairLines('quota-headroom'), []);
  assert.deepEqual(repairLines('expiry-covered'), []);
});
