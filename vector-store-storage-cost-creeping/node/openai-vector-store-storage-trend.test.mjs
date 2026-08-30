import { test } from 'node:test';
import assert from 'node:assert/strict';
import { UNGROUPED, byteSeries, growth, idleStores, querySeries, repairLines,
         searchesByStore, slope, storageLines, verdict, windowStart }
  from './openai-vector-store-storage-trend.mjs';

const GIB = 1073741824;
const DAY = 86400;
const T0 = 1800000000;

const series = (first, last, points = 90, key = 'usage_bytes',
                project = 'proj_research') => {
  const out = [];
  for (let i = 0; i < points; i += 1) {
    const value = first + Math.trunc((last - first) * i / Math.max(points - 1, 1));
    out.push({ start_time: T0 + i * DAY,
               results: [{ [key]: value, project_id: project }] });
  }
  return out;
};

test('bytes tripling with no queries at all is the finding', () => {
  const points = byteSeries(series(Math.trunc(8.1 * GIB),
                                   Math.trunc(31.4 * GIB))).proj_research;
  const [state, detail] = verdict(points, [], 90);
  assert.equal(state, 'bytes-growing-never-queried');
  assert.match(detail, /8\.1 GiB -> 31\.4 GiB/);
  assert.match(detail, /\+288%/);
  const idle = idleStores(
    [{ id: 'vs_c3', name: 'march-demo', usage_bytes: Math.trunc(12.4 * GIB),
       last_active_at: T0 - 148 * DAY }], { vs_c3: 0 }, T0);
  const lines = repairLines(state, idle);
  assert.ok(lines.some((l) => l.includes('march-demo') && l.includes('12.4 GiB')));
  assert.ok(lines.some((l) => l.includes('expiration policy at creation')));
});

test('the same growth with rising queries is not a finding', () => {
  const points = byteSeries(series(44 * GIB, 61 * GIB)).proj_research;
  const queries = querySeries(series(400, 14000, 90, 'num_requests')).proj_research;
  const [state, detail] = verdict(points, queries, 90);
  assert.equal(state, 'bytes-and-queries-growing');
  assert.match(detail, /Growth, priced correctly/);
  assert.ok(repairLines(state)[0].startsWith('nothing to do'));
});

test('the size floor comes before the growth rate', () => {
  const tiny = byteSeries(series(Math.trunc(0.02 * GIB),
                                 Math.trunc(0.12 * GIB))).proj_research;
  const [state, detail] = verdict(tiny, [], 90);
  assert.equal(state, 'below-threshold');
  assert.match(detail, /0\.1 GiB/);
  assert.deepEqual(repairLines(state), []);
});

test('storage is selected by unit and never by name', () => {
  const buckets = [{ results: [
    { line_item: 'Vector store storage', quantity_unit: 'gibibyte_hours',
      quantity: 41288, amount: { value: 412.88, currency: 'usd' } },
    { line_item: 'gpt-5, input', quantity_unit: 'tokens', quantity: 9000000,
      amount: { value: 18402.11, currency: 'usd' } },
    { line_item: 'Storage, renamed next quarter', quantity_unit: 'gibibyte_hours',
      quantity: 10, amount: { value: 0.1, currency: 'usd' } }] }];
  const lines = storageLines(buckets);
  assert.deepEqual(Object.keys(lines).sort(),
                   ['Storage, renamed next quarter', 'Vector store storage']);
  const total = Object.values(lines).reduce((a, v) => a + v.dollars, 0);
  assert.equal(Math.round(total * 100) / 100, 412.98);
  assert.deepEqual(storageLines([]), {});
});

test('an unattributed row never becomes a store', () => {
  const buckets = [{ results: [
    { num_requests: 12, vector_store_id: 'vs_a1', project_id: 'proj_a' },
    { num_requests: 3, vector_store_id: null, project_id: null }] }];
  assert.deepEqual(searchesByStore(buckets), { vs_a1: 12, [UNGROUPED]: 3 });
  assert.deepEqual(byteSeries([{ start_time: T0,
                                 results: [{ usage_bytes: 5, project_id: null }] }]),
                   { [UNGROUPED]: [[T0, 5]] });
});

test('idle stores need real bytes and zero searches', () => {
  const stores = [
    { id: 'vs_big', name: 'corpus', usage_bytes: 9 * GIB,
      last_active_at: T0 - 96 * DAY },
    { id: 'vs_busy', name: 'live', usage_bytes: 9 * GIB, last_active_at: T0 },
    { id: 'vs_small', name: 'scratch', usage_bytes: 40 * 1024 * 1024,
      last_active_at: T0 - 400 * DAY },
    { id: 'vs_never', name: 'no-timestamp', usage_bytes: 2 * GIB,
      last_active_at: null }];
  const rows = idleStores(stores, { vs_busy: 900 }, T0);
  assert.deepEqual(rows.map((r) => r[0]), ['vs_big', 'vs_never']);
  assert.equal(rows[0][3], 96);
  assert.equal(rows[1][3], -1);
  assert.deepEqual(idleStores(null, null, T0), []);
});

test('the slope is zero on a flat series and on one point', () => {
  const flat = [];
  for (let i = 0; i < 30; i += 1) flat.push([T0 + i * DAY, 1000]);
  assert.equal(slope(flat), 0);
  assert.equal(slope([[T0, 5]]), 0);
  assert.equal(slope([]), 0);
  const rising = [];
  for (let i = 0; i < 10; i += 1) rising.push([T0 + i * DAY, 100 * i]);
  assert.equal(Math.round(slope(rising) * 1000) / 1000, 100);
  assert.deepEqual(growth([]), [0, 0, 0, 0]);
  assert.equal(growth([[T0, 0], [T0 + DAY, 50]])[3], 0);
});

test('the window starts at midnight utc', () => {
  assert.equal(windowStart(90, new Date('2026-08-31T17:45:12Z')),
               Date.UTC(2026, 5, 2) / 1000);
});
