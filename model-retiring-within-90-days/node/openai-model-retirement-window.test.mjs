import { test } from 'node:test';
import assert from 'node:assert/strict';
import { plan, trafficNote } from './openai-model-retirement-window.mjs';

const TODAY = new Date('2026-08-30T00:00:00Z');
const dated = (day, id = 'gpt-5-2025-08-07') => ({ id, shutdown_date: day });

test('a date inside the window is due', () => {
  const [state, detail] = plan(dated('2026-11-15'), TODAY);
  assert.equal(state, 'due');
  assert.match(detail, /77 day\(s\) left/);
});

test('a date under a month out is urgent, not merely due', () => {
  const [state, detail] = plan(dated('2026-09-20'), TODAY);
  assert.equal(state, 'urgent');
  assert.match(detail, /not next cycle/);
});

test('a date beyond the window is left alone', () => {
  assert.equal(plan(dated('2027-06-01'), TODAY)[0], 'later');
});

test('the window and the urgency line are both arguments', () => {
  const model = dated('2026-11-15');
  assert.equal(plan(model, TODAY)[0], 'due');
  assert.equal(plan(model, TODAY, 30)[0], 'later');
  assert.equal(plan(model, TODAY, 90, 120)[0], 'urgent');
});

test('a date already passed is out of scope for planning', () => {
  const [state, detail] = plan(dated('2026-07-01'), TODAY);
  assert.equal(state, 'expired');
  assert.match(detail, /already failing/);
});

test('no date is unscheduled rather than safe', () => {
  const [state, detail] = plan({ id: 'gpt-5.6-sol' }, TODAY);
  assert.equal(state, 'unscheduled');
  assert.match(detail, /Re-read/);
  assert.equal(plan({ id: 'x', shutdown_date: 'Q4' }, TODAY)[0], 'unreadable-date');
});

test('unmeasured traffic and zero traffic do not read the same', () => {
  assert.match(trafficNote(null), /no admin key/);
  assert.match(trafficNote(0), /config file/);
  assert.match(trafficNote(4000000), /4000000 request\(s\)/);
  assert.match(plan(dated('2026-09-20'), TODAY, 90, 30, 0)[1], /config file/);
  assert.match(plan(dated('2026-09-20'), TODAY)[1], /no admin key/);
});
