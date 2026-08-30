import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BASE_SHUTDOWN, CUTOFF, WINDOW, createEligibility, daysLeft, familyFor,
         jobVerdict, repairLines, servingDeadline } from './fine-tuning-gate-audit.mjs';

const TODAY = '2026-08-31';

test('a blocked verdict comes from readable state and not an attempt', () => {
  let [state, detail] = createEligibility(TODAY, true, 63);
  assert.equal(state, 'blocked-no-recent-inference');
  assert.ok(detail.includes('63 day(s)'));
  assert.ok(detail.includes('Read from usage, not from an attempt'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('route real traffic')));
  assert.ok(lines.some((l) => l.includes(BASE_SHUTDOWN) && l.includes(CUTOFF)));

  [state, detail] = createEligibility(TODAY, false, 3);
  assert.equal(state, 'blocked-never-fine-tuned');
  assert.ok(detail.includes('Read from the listing, not from an attempt'));
});

test('the window closing is its own state with the days left in it', () => {
  const [state, detail] = createEligibility(TODAY, true, 52);
  assert.equal(state, 'eligibility-expiring');
  assert.ok(detail.includes(`${WINDOW - 52} day(s)`));
  assert.equal(createEligibility(TODAY, true, 12)[0], 'eligible');
  assert.equal(createEligibility('2027-02-01', true, 1)[0], 'create-closed');
  assert.equal(createEligibility('2026-06-01', true, 400)[0], 'eligible');
});

test('the three shapes of the inference clock', () => {
  assert.equal(createEligibility(TODAY, true, 'none-in-window')[0],
               'blocked-no-recent-inference');
  const [state, detail] = createEligibility(TODAY, true, null);
  assert.equal(state, 'unknown-eligibility');
  assert.ok(detail.includes('unknown rather than fine'));
  assert.ok(repairLines(state).some((l) => l.includes('admin-read key')));
});

test('a base is matched exactly or on a hyphen and never loosely', () => {
  const [family, replacement] = familyFor('gpt-4.1-nano-2025-04-14');
  assert.equal(family, 'ft-gpt-4.1-nano-2025-04-14');
  assert.equal(replacement, 'gpt-5.6-luna');
  assert.equal(familyFor('gpt-4')[0], 'ft-gpt-4');
  assert.deepEqual(familyFor('gpt-4-0613'), ['ft-gpt-4', 'gpt-5.6-sol']);
  assert.equal(familyFor('gpt-3.5-turbo-0125')[1], 'gpt-5.6-terra');
  assert.deepEqual(familyFor('gpt-4o-mini-2024-07-18'), [null, null]);
  assert.deepEqual(familyFor(null), [null, null]);
});

test('a date from the api is labelled apart from the published table', () => {
  let [date, source, why] = servingDeadline('2026-12-01', 'ft-gpt-4');
  assert.equal(date, '2026-12-01');
  assert.equal(source, 'api');
  assert.ok(why.includes('read off the model object'));

  [date, source, why] = servingDeadline(null, 'ft-gpt-4');
  assert.equal(date, BASE_SHUTDOWN);
  assert.equal(source, 'published-table');
  assert.ok(why.includes('ft-gpt-4 row in the deprecation table'));

  [date, source] = servingDeadline(null, null);
  assert.equal(date, null);
  assert.equal(source, 'unknown');
});

test('the two verbs are graded apart and can disagree', () => {
  const [create] = createEligibility(TODAY, true, 63);
  let [serve, detail] = jobVerdict('succeeded', 'ft:gpt-4:acme::Ab12', '2027-06-01', TODAY);
  assert.equal(create, 'blocked-no-recent-inference');
  assert.equal(serve, 'serving');
  assert.ok(detail.includes('day(s) of inference left'));

  [serve, detail] = jobVerdict('succeeded', 'ft:gpt-4:acme::Ab12', BASE_SHUTDOWN, TODAY);
  assert.equal(serve, 'dying-soon');
  assert.ok(detail.includes('53 day(s)'));
  const lines = repairLines(serve, 'gpt-5.6-sol');
  assert.ok(lines.some((l) => l.includes('gpt-5.6-sol')));
  assert.ok(lines.some((l) => l.includes('structured outputs')));
});

test('a job with nothing serving and a base with no date', () => {
  assert.equal(jobVerdict('failed', null, BASE_SHUTDOWN, TODAY)[0], 'not-serving');
  assert.equal(jobVerdict('succeeded', null, BASE_SHUTDOWN, TODAY)[0], 'not-serving');
  const [state] = jobVerdict('succeeded', 'ft:x:acme::Zz99', null, TODAY);
  assert.equal(state, 'no-base-date');
  assert.ok(repairLines(state).some((l) => l.includes('undated rather than as safe')));
  assert.equal(jobVerdict('succeeded', 'ft:x:acme::Zz99', '2026-08-01', TODAY)[0],
               'already-dead');
  assert.equal(daysLeft(TODAY, BASE_SHUTDOWN), 53);
});
