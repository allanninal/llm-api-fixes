import { test } from 'node:test';
import assert from 'node:assert/strict';
import { attachmentType, classify, coverage, kmsRef, maskArn, repairLines,
         uncovered, workspaceGeo } from './anthropic-cmek-external-key-audit.mjs';

const ARN = 'arn:aws:kms:eu-west-1:210987654321:key/9f2c';

const key = (id, kind = 'unattached', geo = 'eu', name = 'EU customer key') =>
  ({ id, type: 'external_key', display_name: name, geo,
     attachment: { type: kind },
     provider_config: { type: 'aws', kms_arn: ARN } });

const workspace = (id, keyId = null, geo = 'eu', archived = null) =>
  ({ id, type: 'workspace', name: id, external_key_id: keyId,
     archived_at: archived,
     data_residency: { workspace_geo: geo, default_inference_geo: geo,
                       allowed_inference_geos: 'unrestricted' } });

test('two configs with no live workspace, and only one may be deleted', () => {
  const inert = key('ekey_01hq', 'unattached');
  const holding = key('ekey_01gd', 'attached', 'eu', 'Legacy tenant key');
  const cover = coverage([workspace('wrk_04', 'ekey_01gd', 'eu', 1700000000)]);

  const [stateA, detailA] = classify(inert, cover.ekey_01hq, []);
  assert.equal(stateA, 'unattached-and-unused');
  assert.ok(detailA.includes('inert'));

  const [stateB, detailB] = classify(holding, cover.ekey_01gd, [['wrk_04', 'eu']]);
  assert.equal(stateB, 'archived-workspaces-only');
  assert.ok(detailB.includes('still encrypted under this config'));

  const linesA = repairLines(stateA, inert);
  const linesB = repairLines(stateB, holding);
  assert.ok(linesA.some((l) => l.includes('can be deleted')));
  assert.ok(!linesB.some((l) => l.includes('can be deleted')));
  assert.ok(linesB.some((l) => l.includes('unrecoverable')));
});

test('when the two listings disagree the safe reading wins', () => {
  const stale = key('ekey_01zz', 'unattached');
  const cover = coverage([workspace('wrk_09', 'ekey_01zz')]);
  const [state, detail] = classify(stale, cover.ekey_01zz, [['wrk_09', 'eu']]);
  assert.equal(state, 'unattached-but-referenced');
  assert.ok(detail.includes('The two listings disagree'));
  const lines = repairLines(state, stale);
  assert.ok(lines.some((l) => l.includes('do not delete this')));
  assert.ok(!lines.some((l) => l.includes('can be deleted')));
});

test('an unrecognised attachment is never assumed unattached', () => {
  assert.equal(attachmentType(key('e', 'attached')), 'attached');
  assert.equal(attachmentType(key('e', 'UNATTACHED')), 'unattached');
  assert.equal(attachmentType({ id: 'e', attachment: { type: 'pending' } }), 'unknown');
  assert.equal(attachmentType({ id: 'e' }), 'unknown');
  assert.equal(attachmentType(null), 'unknown');
  const [state, detail] = classify({ id: 'e', attachment: { type: 'pending' } }, {}, []);
  assert.equal(state, 'attachment-unreadable');
  assert.ok(detail.includes('will not say whether'));
});

test('a geo mismatch is read across the workspaces it covers', () => {
  const euKey = key('ekey_01eu', 'attached', 'eu');
  const cover = coverage([workspace('wrk_01', 'ekey_01eu', 'eu'),
                          workspace('wrk_02', 'ekey_01eu', 'us')]);
  const geos = [['wrk_01', 'eu'], ['wrk_02', 'us']];
  const [state, detail] = classify(euKey, cover.ekey_01eu, geos);
  assert.equal(state, 'geo-mismatch');
  assert.ok(detail.includes('wrk_02 at us'));
  assert.ok(!detail.includes('wrk_01'));
  assert.ok(repairLines(state, euKey).some((l) => l.includes('write-once')));
  assert.equal(classify(euKey, cover.ekey_01eu, [['wrk_01', 'eu']])[0], 'covered');
  assert.deepEqual(repairLines('covered', euKey), []);
});

test('the coverage map and the uncovered split', () => {
  const rows = [workspace('wrk_01'), workspace('wrk_02', null),
                workspace('wrk_03', 'ekey_01hq'),
                workspace('wrk_04', 'ekey_01hq', 'eu', 1700000000),
                workspace('wrk_05', null, 'eu', 1700000001)];
  const cover = coverage(rows);
  assert.deepEqual(cover, { ekey_01hq: { live: ['wrk_03'], archived: ['wrk_04'] } });
  assert.equal(Object.keys(cover).length, 1);
  assert.deepEqual(uncovered(rows), [['wrk_01', 'wrk_02'], ['wrk_05']]);
  assert.deepEqual(coverage(null), {});
  assert.deepEqual(uncovered(null), [[], []]);
  assert.equal(workspaceGeo(rows[0]), 'eu');
  assert.equal(workspaceGeo({ id: 'w' }), null);
});

test('the provider line names the key and masks the account', () => {
  assert.equal(maskArn(ARN), 'arn:aws:kms:eu-west-1:****:key/9f2c');
  assert.equal(maskArn('not-an-arn'), 'not-an-arn');
  assert.equal(maskArn(null), 'unknown');
  assert.ok(kmsRef({ type: 'aws', kms_arn: ARN }).startsWith('aws arn:aws:kms:'));
  assert.ok(!kmsRef({ type: 'aws', kms_arn: ARN }).includes('210987654321'));
  assert.equal(kmsRef({ type: 'gcp', key_name: 'projects/p/locations/eu/x' }),
               'gcp projects/p/locations/eu/x');
  assert.ok(kmsRef({ type: 'azure', key_name: 'k',
                     vault_uri: 'https://v.vault.azure.net' })
    .includes('vault.azure.net'));
  assert.equal(kmsRef({ type: 'quantum' }), 'unrecognised provider quantum');
  assert.equal(kmsRef(null), 'unrecognised provider none');
  assert.ok(repairLines('unattached-and-unused', key('ekey_1'))
    .some((l) => l.includes('write verb')));
});
