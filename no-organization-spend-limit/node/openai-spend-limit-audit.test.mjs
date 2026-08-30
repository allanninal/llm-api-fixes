import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectedMonthEnd, thresholdDollars, unknownRecipients, verdict }
  from './openai-spend-limit-audit.mjs';

// The 15th of a 31-day month, so a little under half of it has elapsed.
const NOW = new Date('2026-08-15T12:00:00Z');

const limitOf = (cents, status = 'enforcing') => ({
  object: 'organization.spend_limit', threshold_amount: cents, currency: 'USD',
  interval: 'month', enforcement: { status },
});

const alert = (cents, recipients = ['oncall@example.com']) => ({
  object: 'organization.spend_alert', threshold_amount: cents,
  notification_channel: { type: 'email', recipients },
});

test('threshold is cents, not dollars', () => {
  assert.equal(thresholdDollars(limitOf(90000)), 900);
  assert.equal(thresholdDollars({ spend_limit: limitOf(50000) }), 500);
  assert.equal(thresholdDollars({}), null);
  assert.equal(thresholdDollars(null), null);
  assert.equal(thresholdDollars(limitOf('not a number')), null);
});

test('projection pro-rates against the clock it is given', () => {
  assert.equal(Math.round(projectedMonthEnd(1000, NOW)), 2138);
  const first = new Date('2026-08-01T00:00:00Z');
  assert.equal(Math.round(projectedMonthEnd(10, first)), 10 * 31 * 24);
});

test('no limit at all is the headline finding', () => {
  const [state, detail] = verdict({}, [], 400, NOW);
  assert.equal(state, 'no-limit');
  assert.match(detail, /no spend limit is configured/);
});

test('a limit that is not enforcing is reported before any arithmetic', () => {
  const [state] = verdict(limitOf(90000, 'inactive'), [alert(45000)], 400, NOW);
  assert.equal(state, 'not-enforcing');
});

test('a threshold typed as dollars is named as the cents mistake', () => {
  const [state, detail] = verdict(limitOf(500), [alert(250)], 400, NOW);
  assert.equal(state, 'cents-mistake');
  assert.match(detail, /in cents/);
});

test('already over and on track to go over are different states', () => {
  assert.equal(verdict(limitOf(30000), [alert(15000)], 400, NOW)[0], 'breached');
  assert.equal(verdict(limitOf(70000), [alert(35000)], 400, NOW)[0], 'will-breach');
});

test('a ceiling far above the run rate cannot fire in time', () => {
  const [state, detail] = verdict(limitOf(5000000), [alert(2500000)], 400, NOW);
  assert.equal(state, 'ceiling-too-high');
  assert.match(detail, /five times/);
});

test('a brake with no warning light is its own finding', () => {
  const [state, detail] = verdict(limitOf(200000), [], 400, NOW);
  assert.equal(state, 'no-alerts');
  assert.match(detail, /429/);
});

test('a limit and alerts together is guarded', () => {
  const [state, detail] = verdict(limitOf(200000), [alert(100000), alert(150000)],
    400, NOW);
  assert.equal(state, 'guarded');
  assert.match(detail, /2 alert\(s\)/);
});

test('recipients who left are not an alert', () => {
  const alerts = [alert(1000, ['oncall@example.com', 'Departed@Example.com']),
    alert(2000, ['oncall@example.com'])];
  assert.deepEqual(unknownRecipients(alerts, ['OnCall@example.com']),
    ['Departed@Example.com']);
  assert.deepEqual(
    unknownRecipients(alerts, ['oncall@example.com', 'departed@example.com']), []);
});
