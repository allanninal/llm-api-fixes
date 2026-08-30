import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SHUTDOWN, accessVerdict, cliffVerdict, daysPast, probeState,
         repairLines } from './assistants-shutdown-probe.mjs';

const series = ({ before = 1000, after = 0, lastLive = '2026-08-25' } = {}) =>
  ['2026-08-22', '2026-08-23', '2026-08-24', '2026-08-25',
   '2026-08-26', '2026-08-27', '2026-08-28'].map((d) =>
    (d < SHUTDOWN ? [d, d <= lastLive ? before : 0] : [d, after]));

test('past the date a 200 is the finding and a 404 is the baseline', () => {
  let [state, why] = accessVerdict('gone', 'answering', daysPast('2026-08-31'));
  assert.equal(state, 'shut-down');
  assert.ok(why.includes(SHUTDOWN));

  [state, why] = accessVerdict('answering', 'answering', daysPast('2026-08-31'));
  assert.equal(state, 'grace-access');
  assert.ok(why.includes('grace rather than a supported state'));
  assert.equal(daysPast('2026-08-31'), 5);
  assert.equal(daysPast('2026-08-20'), -6);
});

test('a dead control path can never produce a shutdown verdict', () => {
  const [state, why] = accessVerdict('gone', 'credentials', 5);
  assert.equal(state, 'control-failed');
  assert.ok(why.includes('proves nothing'));
  assert.equal(accessVerdict('gone', 'unreachable', 5)[0], 'control-failed');
  assert.ok(repairLines('control-failed').some((l) => l.includes('re-run')));
});

test('a 429 is a refusal from a path that still exists', () => {
  const [state, why] = probeState(429, { error: { code: 'rate_limit_exceeded' } });
  assert.equal(state, 'throttled');
  assert.ok(why.includes('still routes'));
  assert.equal(accessVerdict('throttled', 'answering', 5)[0], 'grace-access');
  assert.equal(probeState(200, { object: 'list' })[0], 'answering');
  assert.equal(probeState(404, { error: { code: 'model_not_found' } })[0], 'gone');
  assert.equal(probeState(null)[0], 'unreachable');
  assert.equal(probeState(500, {})[0], 'refused');
});

test('a cliff that lands on the date is the shutdown', () => {
  const [state, why] = cliffVerdict(series());
  assert.equal(state, 'cliff-on-the-date');
  assert.ok(why.includes('not a deploy'));
  assert.ok(repairLines(state).some((l) => l.includes('Migrate this project first')));
});

test('a cliff two days early is a deploy and is named as one', () => {
  const [state, why] = cliffVerdict(series({ lastLive: '2026-08-23' }));
  assert.equal(state, 'cliff-elsewhere');
  assert.ok(why.includes('2026-08-23'));
  assert.deepEqual(repairLines(state), []);
});

test('a partial drop is reported as a dip and never rounded up', () => {
  const [state, why] = cliffVerdict(series({ after: 180 }));
  assert.equal(state, 'dip-on-the-date');
  assert.ok(why.includes('18%'));
  assert.ok(why.includes('part was not'));
  assert.equal(cliffVerdict(series({ after: 900 }))[0], 'still-running');
  assert.equal(cliffVerdict([])[0], 'not-checked');
  assert.equal(cliffVerdict([['2026-08-01', 5]])[0], 'window-too-short');
  assert.equal(cliffVerdict(series({ before: 0 }))[0], 'no-traffic-in-window');
});

test('the repair describes a rewrite and names no model id', () => {
  const joined = repairLines('shut-down').join(' ');
  assert.ok(joined.includes('/v1/responses'));
  assert.ok(joined.includes('/v1/conversations'));
  assert.ok(joined.includes('assistants=v2'));
  assert.ok(joined.includes('no successor model id'));
  assert.ok(!joined.includes('gpt-'));
});
