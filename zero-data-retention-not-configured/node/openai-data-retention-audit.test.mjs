import { test } from 'node:test';
import assert from 'node:assert/strict';
import { archived, classify, effective, family, repairLines, residencyNote }
  from './openai-data-retention-audit.mjs';

const EU = { id: 'proj_eu', name: 'EU tenant', status: 'active',
             residency: 'EU_STORAGE_PROCESSING' };

test('the same project resolves two ways under two org defaults', () => {
  let [state, detail] = classify(EU, 'enhanced_zero_data_retention',
                                 'organization_default', 'zdr');
  assert.equal(state, 'inherited-not-pinned');
  assert.ok(detail.includes('only because the organization default says so'));
  assert.ok(repairLines(state, EU, 'zdr')
    .some((l) => l.includes('moves the day somebody changes')));

  [state, detail] = classify(EU, 'modified_abuse_monitoring',
                             'organization_default', 'zdr');
  assert.equal(state, 'weaker-than-claimed');
  assert.ok(detail.includes('inherited from the organization'));
  assert.ok(detail.includes('zero data retention was claimed'));

  assert.equal(classify(EU, 'modified_abuse_monitoring',
                        'zero_data_retention', 'zdr')[0], 'compliant');
});

test('none is never treated as an inherit', () => {
  const [state, detail] = classify({ id: 'proj_ingest' },
    'enhanced_zero_data_retention', 'none', 'zdr');
  assert.equal(state, 'no-retention-control');
  assert.ok(detail.includes('whatever the organization default says'));
  assert.deepEqual(effective('enhanced_zero_data_retention', 'none'), ['none', false]);
  assert.deepEqual(effective('enhanced_zero_data_retention', 'organization_default'),
                   ['enhanced_zero_data_retention', true]);
  assert.deepEqual(effective('zero_data_retention', null), [null, false]);
});

test('the family map groups without ranking', () => {
  assert.equal(family('zero_data_retention'), 'zdr');
  assert.equal(family('enhanced_zero_data_retention'), 'zdr');
  assert.equal(family('modified_abuse_monitoring'), 'modified-abuse-monitoring');
  assert.equal(family('enhanced_modified_abuse_monitoring'),
               'modified-abuse-monitoring');
  assert.equal(family('none'), 'none');
  assert.equal(family(null), 'unreadable');
  assert.equal(family('standard'), 'unrecognised');
  const project = { id: 'proj_a' };
  assert.equal(classify(project, null, 'modified_abuse_monitoring',
                        'modified-abuse-monitoring')[0], 'compliant');
  assert.equal(classify(project, null, 'modified_abuse_monitoring', 'zdr')[0],
               'weaker-than-claimed');
});

test('an unrecognised value is never graded as safe', () => {
  const [state, detail] = classify({ id: 'proj_x' }, 'zero_data_retention',
                                   'legacy_mode', 'zdr');
  assert.equal(state, 'retention-unreadable');
  assert.ok(detail.includes('will not grade as safe'));
  assert.ok(repairLines(state, { id: 'proj_x' })
    .some((l) => l.includes('Read it by hand')));
  assert.equal(classify({ id: 'proj_y' }, 'zero_data_retention', null, 'zdr')[0],
               'retention-unreadable');
});

test('archived projects are graded and labelled', () => {
  const old = { id: 'proj_old', archived_at: 1700000000 };
  assert.equal(archived(old), true);
  assert.equal(archived({ id: 'p', status: 'archived' }), true);
  assert.equal(archived({ id: 'p', status: 'active' }), false);
  const [state, detail] = classify(old, 'modified_abuse_monitoring', 'none', 'zdr');
  assert.equal(state, 'no-retention-control');
  assert.ok(detail.includes('its retained data is still retained'));
});

test('residency is a separate axis and absent is not GLOBAL', () => {
  assert.deepEqual(residencyNote(EU, 'EU_STORAGE_PROCESSING'), [true, null]);
  let [ok, detail] = residencyNote({ id: 'p', residency: 'US_STORAGE_PROCESSING' },
                                   'EU_STORAGE_PROCESSING');
  assert.equal(ok, false);
  assert.ok(detail.includes('residency is US_STORAGE_PROCESSING'));
  [ok, detail] = residencyNote({ id: 'p' }, 'EU_STORAGE_PROCESSING');
  assert.equal(ok, false);
  assert.ok(detail.includes('neither GLOBAL nor'));
  assert.deepEqual(residencyNote({ id: 'p' }, null), [true, null]);
});

test('the repair body uses retention_type and says it is a request', () => {
  const lines = repairLines('no-retention-control', { id: 'proj_ingest' }, 'zdr');
  assert.ok(lines.some((l) => l.includes('{"retention_type": "zero_data_retention"}')));
  assert.ok(lines.some((l) => l.includes('the response field is type')));
  assert.ok(lines.some((l) => l.includes('Request it')));
  assert.deepEqual(repairLines('compliant', { id: 'proj_ingest' }, 'zdr'), []);
});
