import { test } from 'node:test';
import assert from 'node:assert/strict';
import { daysLeft, verdict } from './openai-batch-error-file-audit.mjs';

// 2026-08-30T00:00:00Z. Fixed, because the retention boundary is the point.
const NOW = 1788048000;
const DAY = 86400;

function batch({ status = 'completed', errorFileId = 'file_err',
                 ageDays = 10 } = {}) {
  return {
    id: 'batch_test',
    status,
    error_file_id: errorFileId,
    created_at: NOW - ageDays * DAY,
  };
}

function meta({ size = 4096, ageDays = 10 } = {}) {
  return {
    id: 'file_err',
    bytes: size,
    purpose: 'batch_output',
    created_at: NOW - ageDays * DAY,
  };
}

test('an error file nobody fetched is the finding', () => {
  const [state, detail] = verdict(batch(), meta({ size: 4096 }), new Set(), NOW);
  assert.equal(state, 'unread');
  assert.match(detail, /4096 byte\(s\)/);
  assert.match(detail, /missing from the downstream table/);
});

test('the ingest record is what clears it', () => {
  assert.equal(verdict(batch(), meta(), new Set(['file_err']), NOW)[0], 'fetched');
});

test('retention turns a task into a hole', () => {
  const [near, nearDetail] = verdict(batch({ ageDays: 29 }),
                                     meta({ ageDays: 29 }), new Set(), NOW);
  assert.equal(near, 'expiring');
  assert.match(nearDetail, /1 day\(s\)/);

  const [gone, goneDetail] = verdict(batch({ ageDays: 31 }),
                                     meta({ ageDays: 31 }), new Set(), NOW);
  assert.equal(gone, 'aged-out');
  assert.match(goneDetail, /not retrievable/);
});

test('a missing file object reads differently inside and outside the window', () => {
  assert.equal(verdict(batch({ ageDays: 40 }), null, new Set(), NOW)[0], 'aged-out');
  assert.equal(verdict(batch({ ageDays: 2 }), null, new Set(), NOW)[0],
               'unresolvable');
});

test('an empty error file is not a pile of failures', () => {
  const [state, detail] = verdict(batch(), meta({ size: 0 }), new Set(), NOW);
  assert.equal(state, 'empty');
  assert.match(detail, /never written to/);
});

test('batches with nothing to read are left alone', () => {
  assert.equal(verdict(batch({ errorFileId: null }), null, new Set(), NOW)[0],
               'no-error-file');
  assert.equal(verdict(batch({ errorFileId: '' }), null, new Set(), NOW)[0],
               'no-error-file');
  assert.equal(verdict(batch({ status: 'in_progress' }), meta(), new Set(), NOW)[0],
               'running');
});

test('daysLeft floors and admits ignorance', () => {
  assert.equal(daysLeft(NOW - 10 * DAY, NOW), 20);
  assert.equal(daysLeft(NOW - Math.trunc(29.9 * DAY), NOW), 1);
  assert.equal(daysLeft(NOW - 30 * DAY, NOW), 0);
  assert.equal(daysLeft(null, NOW), null);
  assert.equal(daysLeft('yesterday', NOW), null);
});
