import { test } from 'node:test';
import assert from 'node:assert/strict';
import { daysSince, replacement, verdict } from './anthropic-model-ids-audit.mjs';

const TODAY = new Date('2026-08-30T00:00:00Z');
const LIVE = new Set(['claude-opus-4-8', 'claude-sonnet-4-6',
                      'claude-haiku-4-5-20251001', 'claude-opus-4-1-20250805']);

test('an id in the live list is callable', () => {
  const [state, detail] = verdict('claude-sonnet-4-6', LIVE, TODAY);
  assert.equal(state, 'live');
  assert.match(detail, /live models list/);
});

test('an id missing from the list and on the table is retired', () => {
  const [state, detail] = verdict('claude-3-5-sonnet-20241022', new Set(), TODAY);
  assert.equal(state, 'retired');
  assert.match(detail, /2025-10-28/);
  assert.match(detail, /not_found_error/);
  assert.match(detail, /claude-sonnet-4-6/);
});

test('the days since retirement are counted from the date passed in', () => {
  assert.equal(daysSince('2026-06-15', TODAY), 76);
  assert.equal(daysSince('not a date', TODAY), null);
  assert.match(verdict('claude-opus-4-20250514', new Set(), TODAY)[1],
               /76 day\(s\) ago/);
});

test('missing from the list but not on the table is unknown', () => {
  const [state, detail] = verdict('claude-sonnet-4-6-20260101', new Set(), TODAY);
  assert.equal(state, 'unknown');
  assert.match(detail, /Bedrock/);
});

test('the api wins over the hardcoded table', () => {
  const [state, detail] = verdict('claude-opus-4-1-20250805', LIVE, TODAY);
  assert.equal(state, 'table-stale');
  assert.match(detail, /Trust the API/);
});

test('an empty string is not silently live', () => {
  assert.equal(verdict('', LIVE, TODAY)[0], 'unreadable');
  assert.equal(verdict(null, LIVE, TODAY)[0], 'unreadable');
});

test('the replacement is family level and admits ignorance', () => {
  assert.equal(replacement('claude-3-opus-20240229'), 'claude-opus-4-8');
  assert.equal(replacement('claude-3-5-haiku-20241022'), 'claude-haiku-4-5-20251001');
  assert.equal(replacement('claude-instant-1.2'), 'claude-haiku-4-5-20251001');
  assert.equal(replacement('claude-2.1'), 'claude-sonnet-4-6');
  assert.equal(replacement('some-other-vendor-model'), null);
});
