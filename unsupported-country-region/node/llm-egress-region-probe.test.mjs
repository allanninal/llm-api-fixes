import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BLOCK_CODE, blob, classify, compare, errorCode, loadBaseline,
         observation, repairLines } from './llm-egress-region-probe.mjs';

const blocked = (provider = 'openai') => observation(provider, 403, {
  error: { message: 'Country, region, or territory not supported.',
           type: 'invalid_request_error', code: BLOCK_CODE },
});
const ok = (provider = 'openai') =>
  observation(provider, 200, { data: [], object: 'list' });

test('the pair is what turns a 403 into a statement about geography', () => {
  const [state, detail] = compare(blocked(), ok());
  assert.equal(state, 'geography-isolated');
  assert.ok(detail.includes('not the credential'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes("regions: ['iad1']")));
  assert.ok(lines.some((l) => l.includes('Move the workload, not the packets')));
  assert.ok(!lines.some((l) => l.toLowerCase().includes('proxy the')));
});

test('blocked from both hosts is the account and not this deployment', () => {
  const [state, detail] = compare(blocked(), blocked());
  assert.equal(state, 'region-blocked-everywhere');
  assert.ok(detail.includes('organization-level restriction'));
  assert.ok(repairLines(state).some((l) => l.includes('moving this deployment will not help')));
});

test('a 401 from both hosts is handed to the credential question', () => {
  const unauth = observation('openai', 401, { error: { code: 'invalid_api_key' } });
  const [state, detail] = compare(unauth, unauth);
  assert.equal(state, 'credentials-not-geography');
  assert.ok(detail.includes('not the location'));
  assert.ok(repairLines(state).some((l) => l.includes('not this note')));

  assert.equal(compare(unauth, ok())[0], 'credentials-here-only');
  assert.ok(repairLines('credentials-here-only')
    .some((l) => l.includes('different value in the environment')));
});

test('one observation refuses to conclude even with the documented code', () => {
  const [state, detail] = compare(blocked(), null);
  assert.equal(state, 'region-blocked-unconfirmed');
  assert.ok(detail.includes('has not been separated from an account-level restriction'));
  assert.ok(repairLines(state).some((l) => l.includes('host you already trust')));
  assert.equal(compare(ok(), null)[0], 'clear');
});

test('the blob round trips and carries no credential', () => {
  const line = blob([blocked(), ok('anthropic')]);
  assert.ok(!line.includes('sk-'));
  assert.equal(line, '{"anthropic":{"code":"","status":200},'
    + '"openai":{"code":"unsupported_country_region_territory","status":403}}');
  const back = loadBaseline(line);
  assert.equal(classify(back.openai)[0], 'region-blocked');
  assert.equal(classify(back.anthropic)[0], 'reachable');
  assert.deepEqual(loadBaseline('{not json'), {});
  assert.deepEqual(loadBaseline(null), {});
});

test('an undocumented 403 is recorded rather than attributed', () => {
  const other = observation('anthropic', 403,
    { error: { type: 'permission_error', message: '...' } });
  const [state, detail] = classify(other);
  assert.equal(state, 'forbidden-other');
  assert.ok(detail.includes('permission_error'));
  const [verdict, why] = compare(other, ok('anthropic'));
  assert.equal(verdict, 'forbidden-unexplained');
  assert.ok(why.includes('not one this script can attribute'));
  assert.ok(repairLines(verdict).some((l) => l.includes('supported regions list')));
});

test('bodies are read in either envelope and odd ones do not raise', () => {
  assert.equal(errorCode({ error: { code: 'a', type: 'b' } }), 'a');
  assert.equal(errorCode({ error: { type: 'b' } }), 'b');
  assert.equal(errorCode({ error: 'a string' }), '');
  assert.equal(errorCode(null), '');
  assert.equal(observation('openai', null, null).status, null);
  assert.equal(classify(observation('openai', null, null))[0], 'unreachable');
  assert.equal(classify(observation('openai', 429, null))[0], 'rate-limited');
  assert.deepEqual(repairLines('clear'), []);
});
