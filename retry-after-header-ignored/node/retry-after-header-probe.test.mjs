import { test } from 'node:test';
import assert from 'node:assert/strict';
import { clockSkew, compare, lowerHeaders, missing, parseReset, repairLines,
         staleResets, verdict } from './retry-after-header-probe.mjs';

const ANTHROPIC_OK = {
  'Anthropic-Ratelimit-Requests-Limit': '1000',
  'anthropic-ratelimit-requests-remaining': '998',
  'anthropic-ratelimit-requests-reset': '2026-08-31T09:12:00Z',
  'anthropic-ratelimit-input-tokens-limit': '10000000',
  'anthropic-ratelimit-input-tokens-remaining': '9998000',
  'anthropic-ratelimit-input-tokens-reset': '2026-08-31T09:12:00Z',
  'anthropic-ratelimit-output-tokens-limit': '2000000',
  'anthropic-ratelimit-output-tokens-remaining': '1999000',
  'anthropic-ratelimit-output-tokens-reset': '2026-08-31T09:12:00Z',
  'anthropic-ratelimit-tokens-limit': '12000000',
  'anthropic-ratelimit-tokens-remaining': '11997000',
  'anthropic-ratelimit-tokens-reset': '2026-08-31T09:12:00Z',
  date: 'Mon, 31 Aug 2026 09:11:00 GMT',
};

const without = (headers, prefix) => Object.fromEntries(
  Object.entries(headers).filter(([k]) => !k.toLowerCase().startsWith(prefix)));

test('a gateway that drops the triples is the finding', () => {
  const gateway = without(ANTHROPIC_OK, 'anthropic-ratelimit-input');
  const rows = compare(ANTHROPIC_OK, gateway, 'anthropic');
  const stripped = Object.entries(rows).filter(([, v]) => v[2] === 'stripped')
    .map(([n]) => n);
  assert.deepEqual(stripped, ['anthropic-ratelimit-input-tokens-limit',
                              'anthropic-ratelimit-input-tokens-remaining',
                              'anthropic-ratelimit-input-tokens-reset']);
  const [state, detail] = verdict(rows, [], true, 0, []);
  assert.equal(state, 'headers-stripped');
  assert.match(detail, /do not survive the gateway/);
  const lines = repairLines(state, 'anthropic', stripped);
  assert.ok(lines.some((l) => l.includes('retry-after travels with these')));
  assert.ok(lines.some((l) => l.includes('allowlist')));
});

test('remaining may differ across paths but a limit may not', () => {
  const later = { ...ANTHROPIC_OK,
    'anthropic-ratelimit-requests-remaining': '997',
    'anthropic-ratelimit-requests-reset': '2026-08-31T09:12:01Z' };
  let rows = compare(ANTHROPIC_OK, later, 'anthropic');
  assert.equal(rows['anthropic-ratelimit-requests-remaining'][2], 'intact');
  assert.equal(rows['anthropic-ratelimit-requests-reset'][2], 'intact');
  assert.equal(verdict(rows, [], true, 0, [])[0], 'headers-intact');

  const faked = { ...ANTHROPIC_OK, 'anthropic-ratelimit-requests-limit': '50' };
  rows = compare(ANTHROPIC_OK, faked, 'anthropic');
  assert.equal(rows['anthropic-ratelimit-requests-limit'][2], 'rewritten');
  const [state, detail] = verdict(rows, [], true, 0, []);
  assert.equal(state, 'headers-rewritten');
  assert.match(detail, /generating headers rather than forwarding/);
  assert.ok(repairLines(state).some((l) => l.includes('more dangerous than stripping')));
});

test('the two reset formats are told apart rather than guessed', () => {
  assert.deepEqual(parseReset('2026-08-31T09:12:00Z'), ['absolute', 1788167520]);
  assert.deepEqual(parseReset('6m0s'), ['duration', 360]);
  assert.deepEqual(parseReset('30s'), ['duration', 30]);
  assert.deepEqual(parseReset('1h2m3s'), ['duration', 3723]);
  assert.deepEqual(parseReset('500ms'), ['duration', 0.5]);
  assert.deepEqual(parseReset('12'), ['duration', 12]);
  assert.deepEqual(parseReset(''), ['unknown', null]);
  assert.deepEqual(parseReset('soon'), ['unknown', null]);
});

test('the clock is read against the server and a stale reset is its own state', () => {
  const skew = clockSkew('Mon, 31 Aug 2026 09:11:00 GMT', 1788167502);
  assert.equal(Math.round(skew), 42);
  assert.equal(clockSkew('', 0), null);
  assert.equal(clockSkew('not a date', 0), null);
  const [state, detail] = verdict(compare(ANTHROPIC_OK, ANTHROPIC_OK, 'anthropic'),
                                  [], true, skew, []);
  assert.equal(state, 'clock-skew');
  assert.match(detail, /ahead of/);
  assert.ok(repairLines(state, 'anthropic').some((l) => l.includes('RFC 3339 instants')));
  const stale = staleResets(ANTHROPIC_OK, 'anthropic', 1788167600);
  assert.equal(stale.length, 4);
  assert.equal(stale[0][1], 80);
  assert.equal(verdict(compare(ANTHROPIC_OK, ANTHROPIC_OK, 'anthropic'),
                       [], true, 0, stale)[0], 'reset-in-the-past');
});

test('a transport failure is reported before a clock one', () => {
  const gateway = without(ANTHROPIC_OK, 'anthropic-ratelimit-input');
  const rows = compare(ANTHROPIC_OK, gateway, 'anthropic');
  const stale = staleResets(ANTHROPIC_OK, 'anthropic', 1788167600);
  assert.equal(verdict(rows, [], true, 300, stale)[0], 'headers-stripped');
});

test('openai headers and the no gateway case', () => {
  const openai = {
    'x-ratelimit-limit-requests': '10000',
    'x-ratelimit-remaining-requests': '9999',
    'x-ratelimit-reset-requests': '6m0s',
    'x-ratelimit-limit-tokens': '2000000',
    'x-ratelimit-remaining-tokens': '1999000',
    'x-ratelimit-reset-tokens': '6m0s',
  };
  assert.deepEqual(missing(openai, 'openai'), []);
  assert.deepEqual(staleResets(openai, 'openai', 1788167600), []);
  assert.equal(verdict(compare(openai, openai, 'openai'), [], false, 0, [])[0],
               'headers-intact');
  const bare = missing({}, 'openai');
  assert.equal(bare.length, 6);
  const [state, detail] = verdict(compare({}, {}, 'openai'), bare, false, null, []);
  assert.equal(state, 'headers-absent');
  assert.match(detail, /no gateway configured to blame/);
  assert.ok(repairLines(state).some((l) => l.includes('not attributable yet')));
  assert.deepEqual(lowerHeaders(null), {});
  assert.deepEqual(repairLines('headers-intact'), []);
});
