import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ID_BATCH, chunks, classifyId, epoch, fileRow, human, missingIds,
         parseIds, repairLines } from './anthropic-expired-file-refs.mjs';

const NOW = 1_800_000_000;
const DAY = 86400;

const stamp = (when) => new Date(when * 1000).toISOString().replace(/\.\d+Z$/, 'Z');

const row = (id, expiresInDays = null, hasField = true, size = 2048) => {
  const body = { id, type: 'file', filename: `${id}.pdf`, size_bytes: size,
                 created_at: '2026-01-01T00:00:00Z', downloadable: false };
  if (hasField) {
    body.expires_at = expiresInDays === null ? null
      : stamp(NOW + Math.trunc(expiresInDays * DAY));
  }
  return fileRow(body);
};

test('an expired file still answers and fails every use', () => {
  const [state, detail] = classifyId(row('file_011a', -11.3), NOW, 7);
  assert.equal(state, 'expired');
  assert.ok(detail.includes('expired 11.3 day(s) ago'));
  assert.ok(detail.includes('the metadata still answers'));
  assert.ok(detail.includes('every actual use of this id fails'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('cannot be restored')));
  assert.ok(lines.some((l) => l.includes('DELETE /v1/files/{file_id}')));
  assert.ok(lines.some((l) => l.includes('30 day window')));
});

test('an expiry cannot be extended so the repair never suggests it', () => {
  const [state, detail] = classifyId(row('file_02b7', 4.1), NOW, 7);
  assert.equal(state, 'expiring');
  assert.ok(detail.includes('expires in 4.1 day(s)'));
  assert.ok(detail.includes('cannot be extended'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('set once at upload')));
  assert.ok(lines.some((l) => l.includes('Re-upload before the date')));
  assert.ok(!lines.some((l) => l.includes('extend the')));
  const [live, liveDetail] = classifyId(row('file_05e4', 61.8), NOW, 7);
  assert.equal(live, 'live');
  assert.ok(liveDetail.includes('61.8 day(s)'));
  assert.deepEqual(repairLines(live), []);
});

test('an id the api declines to return is the strongest signal', () => {
  const [state, detail] = classifyId(null, NOW, 7);
  assert.equal(state, 'gone');
  assert.ok(detail.includes('not returned by the ids lookup'));
  assert.ok(detail.includes('30 day metadata window'));
  assert.ok(repairLines(state).some((l) => l.includes('no read will recover')));
  const asked = ['file_01', 'file_02', 'file_03'];
  assert.deepEqual(missingIds(asked, ['file_02']), ['file_01', 'file_03']);
  assert.deepEqual(missingIds(asked, asked), []);
  assert.deepEqual(missingIds([], ['file_09']), []);
});

test('a missing expiry field disables the check rather than passing it', () => {
  const blind = row('file_06f1', null, false);
  assert.equal(blind.expiry_reported, false);
  const [state, detail] = classifyId(blind, NOW, 7);
  assert.equal(state, 'expiry-not-reported');
  assert.ok(detail.includes('could not run'));
  assert.ok(repairLines(state).some((l) => l.includes('files-api-2025-04-14')));
  const perm = row('file_04d2', null);
  assert.equal(perm.expiry_reported, true);
  assert.equal(perm.expires_at, null);
  assert.equal(classifyId(perm, NOW, 7)[0], 'no-expiry');
  assert.ok(repairLines('no-expiry')
    .some((l) => l.includes('never leaves the storage total')));
});

test('batching is a contract and not a performance setting', () => {
  const ids = Array.from({ length: 250 }, (_, n) => `file_${String(n).padStart(3, '0')}`);
  const batched = chunks(ids);
  assert.deepEqual(batched.map((b) => b.length), [100, 100, 50]);
  assert.ok(batched.every((b) => b.length <= ID_BATCH));
  assert.deepEqual(chunks(ids, 500).map((b) => b.length), [100, 100, 50]);
  assert.deepEqual(chunks(['a', 'a', ' a ', 'b', '']), [['a', 'b']]);
  assert.deepEqual(chunks([]), []);
  assert.deepEqual(chunks(null), []);
});

test('the dates and the export survive what is really in them', () => {
  const ids = parseIds('file_01\n\n# exported 2026-08-31\nfile_02  # oldest\n'
    + 'file_01\n   \nfile_03\n');
  assert.deepEqual(ids, ['file_01', 'file_02', 'file_03']);
  assert.deepEqual(parseIds(''), []);
  assert.deepEqual(parseIds(null), []);
  assert.equal(epoch('2023-11-14T22:13:20Z'), 1700000000);
  assert.equal(epoch('2023-11-14T22:13:20.512Z'), 1700000000);
  assert.equal(epoch('2023-11-14T23:13:20+01:00'), 1700000000);
  assert.equal(epoch(null), 0);
  assert.equal(epoch('soon'), 0);
  assert.equal(epoch(true), 0);
  const junk = fileRow({ id: 'file_07', size_bytes: 'big', expires_at: 'soon' });
  assert.equal(junk.size, 0);
  assert.equal(junk.expires_at, null);
  assert.equal(junk.expiry_reported, true);
  assert.equal(fileRow(null).id, '');
  assert.equal(human(2048), '2.0 KiB');
});
