import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseCreated, verdict } from './anthropic-alias-pinning-audit.mjs';

const TODAY = new Date('2026-08-30T00:00:00Z');
const model = (id, created = '2025-09-29T00:00:00Z') =>
  ({ id, created_at: created, type: 'model' });

test('a string that resolves to something else is an alias', () => {
  const [state, detail] = verdict('claude-sonnet-4-5',
                                  model('claude-sonnet-4-5-20250929'), TODAY);
  assert.equal(state, 'alias');
  assert.match(detail, /resolves to claude-sonnet-4-5-20250929/);
  assert.match(detail, /Pin claude-sonnet-4-5-20250929/);
});

test('a dated id that resolves to itself is pinned', () => {
  const [state, detail] = verdict('claude-haiku-4-5-20251001',
                                  model('claude-haiku-4-5-20251001'), TODAY);
  assert.equal(state, 'pinned');
  assert.match(detail, /resolves to itself/);
});

test('a dateless id that resolves to itself is also pinned', () => {
  const [state, detail] = verdict('claude-opus-4-8', model('claude-opus-4-8'), TODAY);
  assert.equal(state, 'pinned-dateless');
  assert.match(detail, /Do not append a date/);
});

test('a 404 says what probably caused it', () => {
  const [state, detail] = verdict('claude-opus-4-8-20260601', null, TODAY);
  assert.equal(state, 'not-found');
  assert.match(detail, /remove it/);
});

test('the age of the resolved snapshot is measured from the date passed in', () => {
  assert.equal(parseCreated('2025-09-29T00:00:00Z').toISOString().slice(0, 10),
               '2025-09-29');
  assert.equal(parseCreated(''), null);
  assert.equal(parseCreated('last autumn'), null);
  const [, detail] = verdict('claude-sonnet-4-5',
                             model('claude-sonnet-4-5-20250929'), TODAY);
  assert.match(detail, /335 day\(s\) ago/);
});

test('a missing created_at drops the age rather than inventing one', () => {
  const [state, detail] = verdict('claude-sonnet-4-5',
                                  { id: 'claude-sonnet-4-5-20250929' }, TODAY);
  assert.equal(state, 'alias');
  assert.ok(!/day\(s\) ago/.test(detail));
});

test('an empty string or a headless object is unreadable', () => {
  assert.equal(verdict('', model('x'), TODAY)[0], 'unreadable');
  assert.equal(verdict('claude-opus-4-8', { created_at: 'x' }, TODAY)[0],
               'unreadable');
});
