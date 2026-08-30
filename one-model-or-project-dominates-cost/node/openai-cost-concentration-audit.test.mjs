import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rank, unitPrice, verdict } from './openai-cost-concentration-audit.mjs';

function row({ name = 'gpt-5.6-sol, input', amount = 0, quantity = 0,
               unit = 'tokens' } = {}) {
  return { name, amount, quantity, unit };
}

function result({ lineItem = 'gpt-5.6-sol, input', value = 0, quantity = 0,
                  unit = 'tokens', project = null } = {}) {
  return { line_item: lineItem, project_id: project,
           amount: { value, currency: 'usd' },
           quantity, quantity_unit: unit };
}

function bucket(...results) {
  return { start_time: 0, end_time: 86400, results };
}

test('one row carrying most of the bill is the finding', () => {
  const [state, detail] = verdict([row({ amount: 7800 }),
                                   row({ name: 'b', amount: 1500 }),
                                   row({ name: 'c', amount: 700 })]);
  assert.equal(state, 'dominant');
  assert.match(detail, /78% of \$10000\.00/);
  assert.match(detail, /at most 22% of the bill/);
});

test('two large rows are not one dominant row', () => {
  const [state, detail] = verdict([row({ name: 'a', amount: 4000 }),
                                   row({ name: 'b', amount: 3800 }),
                                   row({ name: 'c', amount: 2200 })]);
  assert.equal(state, 'top-heavy');
  assert.match(detail, /78% of \$10000\.00 between them/);
});

test('a bill with no lever in it is an answer', () => {
  const rows = [0, 1, 2, 3, 4].map((i) => row({ name: String(i), amount: 2000 }));
  const [state, detail] = verdict(rows);
  assert.equal(state, 'spread');
  assert.match(detail, /across 5 row\(s\)/);
  assert.equal(verdict([])[0], 'no-spend');
  assert.equal(verdict([row({ amount: 0.4 })])[0], 'no-spend');
});

test('a null top row is unattributable rather than unknown', () => {
  const [state, detail] = verdict([row({ name: null, amount: 9000 }),
                                   row({ name: 'b', amount: 1000 })]);
  assert.equal(state, 'unattributable');
  assert.match(detail, /no name/);
  assert.match(detail, /Null is not unknown/);
  assert.equal(verdict([row({ name: 'a', amount: 6000 }),
                        row({ name: null, amount: 4000 })])[0], 'dominant');
});

test('the unit price is only computed for token units', () => {
  assert.equal(unitPrice(200, 50000000, 'tokens'), 4.0);
  assert.equal(unitPrice(200, 50000, '1000_tokens'), 4.0);
  assert.equal(unitPrice(20, 4, 'images'), null);
  assert.equal(unitPrice(20, 4, 'duration_hours'), null);
  assert.equal(unitPrice(20, 0, 'tokens'), null);
  assert.equal(unitPrice(20, 100, null), null);
  assert.equal(unitPrice(20, 100, 'mixed'), null);
});

test('ranking sums across buckets and keeps a null name null', () => {
  const rows = rank([
    bucket(result({ value: 60, quantity: 15000000 }),
           result({ lineItem: 'gpt-5.6-luna, input', value: 10, quantity: 1000000 })),
    bucket(result({ value: 30, quantity: 7500000 }),
           result({ lineItem: null, value: 5, quantity: 0, unit: null })),
  ], 'line_item');
  assert.deepEqual(rows.map((r) => r.name),
    ['gpt-5.6-sol, input', 'gpt-5.6-luna, input', null]);
  assert.equal(rows[0].amount, 90);
  assert.equal(rows[0].quantity, 22500000);
  assert.equal(rows[0].share, 0.8571);
  assert.equal(rows[2].name, null);
  assert.equal(rows[2].unit, null);
});

test('mixed units in one row are reported as mixed not guessed', () => {
  const rows = rank([bucket(result({ value: 1, quantity: 10, unit: 'tokens' }),
                            result({ value: 1, quantity: 2, unit: 'images' }))],
                    'line_item');
  assert.equal(rows[0].unit, 'mixed');
  assert.equal(unitPrice(rows[0].amount, rows[0].quantity, rows[0].unit), null);
});
