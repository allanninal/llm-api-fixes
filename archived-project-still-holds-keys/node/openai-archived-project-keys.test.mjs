import { test } from 'node:test';
import assert from 'node:assert/strict';
import { coversArchived, verdict } from './openai-archived-project-keys.mjs';

const NOW = 1_756_000_000;
const ARCHIVED_AT = NOW - 120 * 86400;

const project = (over = {}) => ({
  id: 'proj_x', name: 'prototype', status: 'archived', archived_at: ARCHIVED_AT, ...over,
});
const key = (lastUsedAt = null, over = {}) => ({
  id: 'key_1', redacted_value: 'sk-proj-...9f2c', last_used_at: lastUsedAt, ...over,
});

test('a listing without the parameter does not cover archived', () => {
  assert.equal(coversArchived({ limit: 100 }), false);
  assert.equal(coversArchived(), false);
});

test('the string false is not truthy here', () => {
  assert.equal(coversArchived({ include_archived: 'false' }), false);
  assert.equal(coversArchived({ include_archived: false }), false);
});

test('the parameter is recognised in the spellings that reach the API', () => {
  assert.equal(coversArchived({ include_archived: 'true' }), true);
  assert.equal(coversArchived({ include_archived: 'TRUE' }), true);
  assert.equal(coversArchived({ include_archived: true }), true);
  assert.equal(coversArchived({ include_archived: '1' }), true);
});

test('an active project is out of scope', () => {
  assert.equal(
    verdict(project({ status: 'active', archived_at: null }), [key(NOW)], NOW)[0],
    'active');
});

test('an archived project with no keys is clean', () => {
  assert.equal(verdict(project(), [], NOW)[0], 'clean');
});

test('a key used after the archive is the urgent case', () => {
  const [state, detail] = verdict(project(), [key(ARCHIVED_AT + 10 * 86400)], NOW);
  assert.equal(state, 'still-serving');
  assert.match(detail, /closed on paper/);
});

test('a key last used before the archive is dead weight', () => {
  const [state, detail] = verdict(project(), [key(ARCHIVED_AT - 5 * 86400)], NOW);
  assert.equal(state, 'live-keys');
  assert.match(detail, /since the archive/);
});

test('a never used key is still reported', () => {
  const [state, detail] = verdict(project(), [key(null)], NOW);
  assert.equal(state, 'dormant-keys');
  assert.match(detail, /has ever authenticated/);
});

test('status archived without a timestamp is still archived', () => {
  assert.equal(verdict(project({ archived_at: null }), [key(NOW - 86400)], NOW)[0],
               'live-keys');
});
