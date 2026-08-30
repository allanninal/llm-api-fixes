import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ABSENT, CURRENT, INITIAL, classifyStatus, declaredFindings,
         gatewayVerdict, hostVerdict, probeHeaders, probeLabels,
         repairLines } from './anthropic-version-header-probe.mjs';

const matrix = ({ absent = 400, current = 200, ancient = 200, ...extra } = {}) =>
  ({ [ABSENT]: absent, [CURRENT]: current, [INITIAL]: ancient, ...extra });

test('the pair of probes is what proves the header is required', () => {
  let [state, detail] = hostVerdict(matrix());
  assert.equal(state, 'version-enforced');
  assert.ok(detail.includes('400 without the header'));

  [state, detail] = hostVerdict(matrix({ absent: 200 }));
  assert.equal(state, 'version-not-enforced');
  assert.ok(detail.includes('gateway on this path is adding it'));
  assert.ok(classifyStatus(ABSENT, 200)[1].endsWith('supplying one for you'));
  assert.ok(repairLines(state).some((l) => l.includes('does not have')));
});

test('a gateway that injects the header is only visible from two hosts', () => {
  const [state, detail] = gatewayVerdict(matrix({ absent: 400 }), matrix({ absent: 200 }));
  assert.equal(state, 'gateway-injects');
  assert.ok(detail.includes('Every client behind it is untested'));
  const lines = repairLines(state);
  assert.ok(lines.some((l) => l.includes('in the client itself')));
  assert.ok(lines.some((l) => l.includes('official SDK')));
});

test('a gateway that strips the header is the mirror case', () => {
  const [state, detail] = gatewayVerdict(matrix(), matrix({ current: 400 }));
  assert.equal(state, 'gateway-strips');
  assert.ok(detail.includes('stripped or rewritten in transit'));
  assert.equal(gatewayVerdict(matrix(), matrix())[0], 'gateway-agrees');
  const [nostate, nodetail] = gatewayVerdict(matrix(), {});
  assert.equal(nostate, 'no-gateway');
  assert.ok(nodetail.includes('invisible to a single host'));
});

test('a matrix you cannot authenticate is not evidence about a header', () => {
  const [state, detail] = hostVerdict(matrix({ absent: 401, current: 401 }));
  assert.equal(state, 'current-rejected');
  assert.ok(detail.includes('credential problem'));
  assert.equal(classifyStatus(ABSENT, 401)[0], 'credentials');
  assert.equal(hostVerdict(matrix({ current: null }))[0], 'unreachable');
  assert.equal(hostVerdict({})[0], 'unreachable');
});

test('the absent probe really sends no version header', () => {
  assert.deepEqual(probeHeaders(ABSENT), {});
  assert.deepEqual(probeHeaders(CURRENT), { 'anthropic-version': '2023-06-01' });
  assert.deepEqual(probeHeaders('  2023-01-01 '), { 'anthropic-version': '2023-01-01' });
  assert.deepEqual(probeLabels([]), [ABSENT, CURRENT, INITIAL]);
  assert.deepEqual(probeLabels(['2023-06-01', ' ', '2024-06-01', '2024-06-01']),
                   [ABSENT, CURRENT, INITIAL, '2024-06-01']);
});

test('declared versions are graded against the history not the status', () => {
  const rows = declaredFindings(matrix({ '2024-06-01': 400 }),
                                [CURRENT, INITIAL, '2024-06-01']);
  const states = Object.fromEntries(rows.map(([v, s]) => [v, s]));
  assert.equal(states[CURRENT], undefined);
  assert.equal(states[INITIAL], 'ancient-pinned');
  assert.equal(states['2024-06-01'], 'unknown-version-pinned');
  const ancient = rows.find(([v]) => v === INITIAL)[2];
  assert.ok(ancient.includes('data: [DONE]'));
  assert.ok(ancient.includes('this host returns 200 for it'));
  assert.ok(repairLines('ancient-pinned').some((l) => l.includes('2023-06-01')));
});

test('single statuses are described and never promoted to verdicts', () => {
  assert.equal(classifyStatus(INITIAL, 200)[0], 'accepted-deprecated');
  assert.equal(classifyStatus(INITIAL, 410)[0], 'refused');
  assert.equal(classifyStatus('2024-06-01', 200)[0], 'accepted-unknown');
  assert.equal(classifyStatus(CURRENT, 529)[0], 'unexpected');
  assert.equal(classifyStatus(CURRENT, null)[0], 'unreachable');
  assert.deepEqual(repairLines('version-enforced'), []);
});
