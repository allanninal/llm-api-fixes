import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, dailyFromAnthropic, dailyFromOpenai, parseCents, weeks }
  from './llm-spend-week-over-week.mjs';

function dollarsPerDay(from, to, amount) {
  const daily = {};
  for (let day = from; day <= to; day += 1) {
    daily[`2026-08-${String(day).padStart(2, '0')}`] = amount;
  }
  return daily;
}

test('today is never counted in the newest week', () => {
  const got = weeks(dollarsPerDay(1, 15, 1.0), '2026-08-15');
  assert.equal(got.length, 2);
  assert.deepEqual(got[0], ['2026-08-08', '2026-08-14', 7.0]);
  assert.deepEqual(got[1], ['2026-08-01', '2026-08-07', 7.0]);
});

test('a partial oldest week is dropped rather than reported short', () => {
  const got = weeks(dollarsPerDay(1, 11, 10.0), '2026-08-12');
  assert.deepEqual(got.map((w) => w[2]), [70.0]);
});

test('one high week is a spike and two are a step', () => {
  const [spike, spikeDetail] = classify([3000, 1000, 1000, 1000]);
  assert.equal(spike, 'spike');
  assert.match(spikeDetail, /a job that ran/);

  const [step, stepDetail] = classify([3000, 3000, 1000, 1000]);
  assert.equal(step, 'step');
  assert.match(stepDetail, /held for two weeks/);
});

test('a ramp is caught even though week over week never trips', () => {
  const [state, detail] = classify([1520.88, 1322.5, 1150.0, 1000.0]);
  assert.equal(state, 'ramp');
  assert.match(detail, /already in the baseline/);
  assert.equal(classify([1000, 1000, 1000, 1000])[0], 'flat');
});

test('spend falling off a cliff is reported rather than celebrated', () => {
  const [state, detail] = classify([400, 1000, 1000, 1000]);
  assert.equal(state, 'drop');
  assert.match(detail, /traffic that stopped/);
});

test('a short history and a standing start are their own answers', () => {
  assert.equal(classify([5000, 10])[0], 'too-short');
  assert.equal(classify([500, 0, 0])[0], 'new-spend');
  assert.equal(classify([0, 0, 0])[0], 'no-spend');
  assert.equal(classify(['lots', 1, 2])[0], 'unreadable');
});

test('anthropic cents are parsed exactly and not as floats', () => {
  assert.equal(parseCents('1234.5'), 1234500);
  assert.equal(parseCents('0.001'), 1);
  assert.equal(parseCents('-250'), -250000);
  assert.equal(parseCents(''), null);
  assert.equal(parseCents(null), null);
  assert.equal(parseCents('1,234'), null);
  assert.equal(parseCents('lots'), null);
});

test('both providers fold into the same day keyed dollars', () => {
  const openai = dailyFromOpenai([{
    start_time: 1785542400,
    end_time: 1785628800,
    results: [{ amount: { value: 12.5, currency: 'usd' } },
              { amount: { value: 0.25, currency: 'usd' } }],
  }]);
  assert.deepEqual([...openai], [['2026-08-01', 12.75]]);

  const anthropic = dailyFromAnthropic([{
    starting_at: '2026-08-01T00:00:00Z',
    results: [{ amount: '1250.0' }, { amount: '25' }],
  }]);
  assert.deepEqual([...anthropic], [['2026-08-01', 12.75]]);
  assert.deepEqual([...dailyFromAnthropic([{ starting_at: 'nonsense',
                                             results: [{ amount: '1' }] }])], []);
});
