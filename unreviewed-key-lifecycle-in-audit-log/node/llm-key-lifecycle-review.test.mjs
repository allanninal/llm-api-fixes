import { test } from 'node:test';
import assert from 'node:assert/strict';
import { failedLoginBursts, feedState, grade, hourOf, iso, normaliseAnthropic,
         normaliseOpenai, parseWhen, projectCaveat, resolveActor, watermark }
  from './llm-key-lifecycle-review.mjs';

const ROSTER = new Set(['dana@example.test', 'marco@example.test']);
const COUNTRIES = ['US', 'GB'];
const at = (text) => Math.floor(new Date(text).getTime() / 1000);

const sessionEntry = (type, when, email, ip = '198.51.100.24', country = 'US') => ({
  id: 'audit_1', type, effective_at: when,
  project: { id: 'proj_1', name: 'prod' },
  actor: { type: 'session',
           session: { user: { email }, ip_address: ip,
                      ip_address_details: { country } } },
});

test('a key minted at 2am by somebody who has left trips three rules', () => {
  const event = normaliseOpenai(sessionEntry(
    'api_key.created', at('2026-03-17T02:14:08Z'), 'ada@example.test',
    '198.51.100.24', 'NL'));
  const [state, reasons] = grade(event, ROSTER, [7, 19], COUNTRIES);
  assert.equal(state, 'off-roster-actor');
  assert.equal(reasons.length, 3);
  assert.ok(reasons.some((r) => r.includes('not on the current roster')));
  assert.ok(reasons.some((r) => r.includes('outside the operating geographies')));
  assert.ok(reasons.some((r) => r.includes('02:00 UTC')));
  assert.equal(iso(event.when), '2026-03-17T02:14:08Z');
});

test('an empty feed is unavailable and never clean', () => {
  const [emptyState, emptyDetail] = feedState([], true);
  assert.equal(emptyState, 'feed-unavailable');
  assert.match(emptyDetail, /not a clean result/);
  const [unreachableState, unreachableDetail] = feedState([], false);
  assert.equal(unreachableState, 'feed-unavailable');
  assert.match(unreachableDetail, /could not be read/);
  const [okState, okDetail] = feedState([{ type: 'api_key.created' }], true);
  assert.equal(okState, 'feed-readable');
  assert.match(okDetail, /1 event\(s\)/);
});

test('the two openai actor shapes keep their email in different places', () => {
  const session = normaliseOpenai(sessionEntry(
    'api_key.created', at('2026-08-11T10:02:00Z'), 'Dana@Example.test'));
  assert.equal(session.actorKind, 'session');
  assert.equal(session.actorEmail, 'dana@example.test');
  assert.equal(session.country, 'US');
  assert.equal(grade(session, ROSTER, [7, 19], COUNTRIES)[0], 'reviewed');

  const byKey = normaliseOpenai({
    type: 'api_key.deleted', effective_at: at('2026-08-02T11:40:55Z'),
    project: { id: 'proj_default' },
    actor: { type: 'api_key',
             api_key: { id: 'key_track', service_account: { id: 'svc_1' } } } });
  assert.equal(byKey.actorKind, 'api_key');
  assert.equal(byKey.actorEmail, null);
  assert.equal(resolveActor(byKey, ROSTER), 'unattributable');
  const [state, reasons] = grade(byKey, ROSTER, [7, 19], COUNTRIES);
  assert.equal(state, 'unattributable');
  assert.ok(reasons.some((r) => r.includes('no user email')));
  assert.match(projectCaveat(byKey), /default project/);
  assert.equal(projectCaveat(session), null);
});

test('an anthropic activity has no country so the rule is skipped', () => {
  const event = normaliseAnthropic({
    type: 'api_key.created', created_at: '2026-08-14T09:31:00Z',
    organization_id: 'org_1',
    actor: { email_address: 'MARCO@example.test', user_id: 'u_1',
             ip_address: '203.0.113.9', user_agent: 'curl/8' } });
  assert.equal(event.source, 'anthropic');
  assert.equal(event.country, null);
  assert.equal(event.actorEmail, 'marco@example.test');
  assert.equal(grade(event, ROSTER, [7, 19], COUNTRIES)[0], 'reviewed');
  assert.equal(projectCaveat(event), null);
  const anonymous = normaliseAnthropic({ type: 'api_key.deleted',
                                         created_at: '2026-08-14T09:31:00Z' });
  assert.equal(anonymous.actorEmail, null);
  assert.equal(resolveActor(anonymous, ROSTER), 'unattributable');
});

test('timestamps arrive in two shapes and the hour is utc', () => {
  assert.equal(parseWhen(1772000000), 1772000000);
  assert.equal(parseWhen('2026-03-17T02:14:08Z'), at('2026-03-17T02:14:08Z'));
  assert.equal(parseWhen('1772000000'), 1772000000);
  assert.equal(parseWhen(null), null);
  assert.equal(parseWhen(true), null);
  assert.equal(parseWhen('whenever'), null);
  assert.equal(hourOf({ when: at('2026-03-17T02:14:08Z') }), 2);
  assert.equal(hourOf({}), null);
  assert.equal(iso(null), '(no timestamp)');
});

test('a burst of failed logins and the watermark for the next run', () => {
  const base = at('2026-08-20T09:00:00Z');
  const events = Array.from({ length: 6 }, (_, i) => (
    { type: 'login.failed', when: base + i * 60, actorEmail: 'ada@example.test' }));
  events.push({ type: 'api_key.created', when: base + 4000,
                actorEmail: 'dana@example.test' });
  const bursts = failedLoginBursts(events);
  assert.equal(bursts.length, 1);
  assert.equal(bursts[0][0], base);
  assert.ok(bursts[0][1] >= 5);
  assert.equal(bursts[0][2], 'ada@example.test');
  assert.deepEqual(failedLoginBursts([events[0]]), []);
  assert.deepEqual(failedLoginBursts([]), []);
  assert.equal(watermark(events), base + 4000);
  assert.equal(watermark([]), null);
  assert.equal(watermark([{ type: 'x' }]), null);
});

test('an out of hours read event is not a creation', () => {
  const updated = { source: 'openai', type: 'api_key.updated',
                    when: at('2026-08-20T03:00:00Z'), actorKind: 'session',
                    actorEmail: 'dana@example.test', actorIp: '203.0.113.1',
                    country: 'US' };
  assert.equal(grade(updated, ROSTER, [7, 19], COUNTRIES)[0], 'reviewed');
  const created = { ...updated, type: 'service_account.created' };
  const [state, reasons] = grade(created, ROSTER, [7, 19], COUNTRIES);
  assert.equal(state, 'out-of-hours');
  assert.deepEqual(reasons, ['created outside business hours (03:00 UTC)']);
});
