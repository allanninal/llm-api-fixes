import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseDay, successor, verdict } from './openai-model-shutdown-audit.mjs';

const TODAY = new Date('2026-08-30T00:00:00Z');

test('shutdown_date is read as a plain day', () => {
  assert.equal(parseDay('2026-12-11').toISOString().slice(0, 10), '2026-12-11');
  assert.equal(parseDay('2026-12-11T00:00:00Z').toISOString().slice(0, 10),
               '2026-12-11');
  assert.equal(parseDay(''), null);
  assert.equal(parseDay(null), null);
  assert.equal(parseDay('December 2026'), null);
});

test('a date already passed is retired', () => {
  const [state, detail] = verdict(
    { id: 'gpt-4-turbo', shutdown_date: '2026-06-15' }, TODAY);
  assert.equal(state, 'retired');
  assert.match(detail, /76 day\(s\) ago/);
  assert.match(detail, /misspelled/);
});

test('a shutdown date of today is its own state', () => {
  const [state, detail] = verdict(
    { id: 'gpt-5-2025-08-07', shutdown_date: '2026-08-30' }, TODAY);
  assert.equal(state, 'retiring-today');
  assert.match(detail, /outage in progress/);
});

test('a future date belongs to the other note', () => {
  const [state, detail] = verdict(
    { id: 'gpt-5-2025-08-07', shutdown_date: '2026-12-11' }, TODAY);
  assert.equal(state, 'scheduled');
  assert.match(detail, /103 day\(s\)/);
});

test('no shutdown date is not a promise', () => {
  const [state, detail] = verdict({ id: 'gpt-5.6-sol', shutdown_date: null }, TODAY);
  assert.equal(state, 'open');
  assert.match(detail, /not a guarantee/);
  assert.equal(verdict({ id: 'gpt-5.6-sol' }, TODAY)[0], 'open');
});

test('an unreadable date is not silently healthy', () => {
  assert.equal(verdict({ id: 'x', shutdown_date: 'soon' }, TODAY)[0],
               'unreadable-date');
  assert.equal(verdict({ shutdown_date: '2026-01-01' }, TODAY)[0], 'unreadable');
});

test('the successor is family level and admits ignorance', () => {
  assert.equal(successor('gpt-5-mini-2025-08-07'), 'gpt-5.6-terra');
  assert.equal(successor('gpt-5-2025-08-07'), 'gpt-5.6-sol');
  assert.equal(successor('dall-e-3'), 'gpt-image-2');
  assert.equal(successor('some-vendor-model'), null);
});
