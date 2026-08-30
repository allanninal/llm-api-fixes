import { test } from 'node:test';
import assert from 'node:assert/strict';
import { REPLACEMENTS, SHUTDOWN, SORA_IDS, assetDeadline, daysLeft, isoDay,
         modelVerdict, repairLines, replacementFor,
         spendVerdict } from './sora-shutdown-inventory.mjs';

const TODAY = '2026-08-31';

test('there is no successor and the script refuses to invent one', () => {
  assert.deepEqual(REPLACEMENTS, {});
  for (const id of SORA_IDS) assert.equal(replacementFor(id), undefined);
  const joined = repairLines('shutdown-dated').join(' ');
  assert.ok(joined.includes('no successor model id'));
  assert.ok(joined.includes('capability leaving the API'));
  assert.ok(joined.includes('third-party provider or dropping the feature'));
});

test('an asset that expires first is on the earlier clock', () => {
  const [state, deadline, detail] = assetDeadline('2026-09-02', TODAY);
  assert.equal(state, 'expires-first');
  assert.equal(deadline, '2026-09-02');
  assert.ok(detail.includes('22 day(s) before the endpoint closes'));
  assert.ok(repairLines(state).some((l) => l.includes('front of the queue')));
});

test('an asset that outlives its expiry still dies with the endpoint', () => {
  let [state, deadline, detail] = assetDeadline('2026-12-01', TODAY);
  assert.equal(state, 'outlives-the-endpoint');
  assert.equal(deadline, SHUTDOWN);
  assert.ok(detail.includes('the endpoint closes first'));

  [state, deadline, detail] = assetDeadline(null, TODAY);
  assert.equal(state, 'no-asset-expiry');
  assert.equal(deadline, SHUTDOWN);
  assert.ok(detail.includes('dies with the endpoint'));
  assert.ok(repairLines(state).some((l) => l.includes('inherit')));
});

test('an expiry already past means the bytes are gone', () => {
  const [state, deadline, detail] = assetDeadline('2026-08-04', TODAY);
  assert.equal(state, 'already-expired');
  assert.equal(deadline, '2026-08-04');
  assert.ok(detail.includes('already unreachable'));
  assert.equal(assetDeadline(TODAY, TODAY)[0], 'already-expired');
});

test('unix stamps become days and bad ones become nothing', () => {
  assert.equal(isoDay(1788000000), '2026-08-29');
  assert.equal(isoDay(null), null);
  assert.equal(isoDay(0), null);
  assert.equal(isoDay('not a stamp'), null);
  assert.equal(daysLeft(TODAY), 24);
  assert.equal(daysLeft('2026-10-01'), -7);
});

test('a stated shutdown date is graded apart from a missing one', () => {
  let [state, detail] = modelVerdict('sora-2', 200, SHUTDOWN, TODAY);
  assert.equal(state, 'shutdown-dated');
  assert.ok(detail.includes('24 day(s) away'));

  [state, detail] = modelVerdict('sora-2', 200, null, TODAY);
  assert.equal(state, 'no-date-from-api');
  assert.ok(detail.includes('published table is the only source'));

  assert.equal(modelVerdict('sora-2', 404, null, TODAY)[0], 'already-gone');
  assert.equal(modelVerdict('sora-2', 401, null, TODAY)[0], 'unreadable');
  assert.equal(modelVerdict('sora-2', null, null, TODAY)[0], 'unreachable');
  assert.equal(modelVerdict('sora-2', 200, '2026-08-01', TODAY)[0], 'past-shutdown');
});

test('spend is a proxy and says so in the case that looks like an all clear', () => {
  let [state, total, detail] = spendVerdict(
    [['Video generation', 400.5], ['sora-2-pro', 12.3], ['Text tokens', 99]], 30);
  assert.equal(state, 'video-spend-accruing');
  assert.equal(Math.round(total * 100) / 100, 412.8);
  assert.ok(detail.includes('412.80'));

  [state, total, detail] = spendVerdict([['Text tokens', 99]], 30);
  assert.equal(state, 'no-video-spend');
  assert.equal(total, 0);
  assert.ok(detail.includes('That is a proxy'));
  assert.deepEqual(repairLines(state), []);
});
