import { test } from 'node:test';
import assert from 'node:assert/strict';
import { arrivalPositions, bestSplit, cacheMinimum, classify, dailyRows, dayKey,
         floorNote, handoff, inputShareAfter, previousModel, repairLines,
         stepAt, sustained }
  from './anthropic-cache-step-after-model-switch.mjs';

const OLD = 'claude-opus-5';
const NEW = 'claude-haiku-4-5-20251001';

const day = (position, share, models) => {
  const total = Object.values(models).reduce((s, v) => s + v, 0);
  const reads = Math.round(total * share);
  return { day: `2026-08-${String(position + 1).padStart(2, '0')}`, position,
           share, reads, uncached: total - reads, writes: 0,
           byModel: { ...models } };
};

const switched = ({ before = 0.70, cold = 0.20, after = 0.10, at = 15,
                    newShare = 1.0, days = 31 } = {}) => {
  const rows = [];
  for (let position = 0; position < days; position += 1) {
    if (position < at) {
      rows.push(day(position, before, { [OLD]: 40000000 }));
    } else {
      const mix = { [NEW]: Math.trunc(40000000 * newShare) };
      if (newShare < 1.0) mix[OLD] = 40000000 - mix[NEW];
      rows.push(day(position, position === at ? cold : after, mix));
    }
  }
  return rows;
};

const STEP = switched();

test('the step aligned with the arrival is the finding', () => {
  assert.deepEqual([...arrivalPositions(STEP)], [[NEW, 15]]);
  assert.equal(Number(inputShareAfter(STEP, NEW, 15).toFixed(3)), 1);

  const shares = STEP.map((r) => r.share);
  const [before, after, delta] = stepAt(shares, 15);
  assert.equal(Number(before.toFixed(2)), 0.70);
  assert.equal(Number(after.toFixed(2)), 0.10);
  assert.equal(Number(delta.toFixed(2)), 0.60);
  assert.equal(bestSplit(shares)[0], 15);
  assert.equal(sustained(shares, 15), true);

  const [state, detail] = classify(STEP);
  assert.equal(state, 'collapsed-after-model-change');
  assert.match(detail, /70% before claude-haiku-4-5-20251001 arrived on 2026-08-16/);
  assert.match(detail, /10% after, with the switch day itself excluded/);
  assert.match(detail, /largest step in the window is exactly there/);
  assert.equal(handoff(state), '');
});

test('a dip that recovers is the cold cache doing its job', () => {
  const recovered = switched({ after: 0.70 });
  assert.equal(recovered.map((r) => r.share)[15], 0.20);
  const [state, detail] = classify(recovered);
  assert.equal(state, 'expected-cold-start');
  assert.match(detail, /dipped to 20% that day and settled back at 70%/);
  assert.match(handoff(state), /not a finding/);
});

test('a collapse somewhere else is not the switch', () => {
  const rows = [];
  for (let position = 0; position < 31; position += 1) {
    const models = position < 5 ? { [OLD]: 40000000 } : { [NEW]: 40000000 };
    rows.push(day(position, position < 20 ? 0.70 : 0.10, models));
  }
  const shares = rows.map((r) => r.share);
  assert.deepEqual([...arrivalPositions(rows)], [[NEW, 5]]);
  assert.equal(bestSplit(shares)[0], 20);

  const [state, detail] = classify(rows);
  assert.equal(state, 'step-elsewhere');
  assert.match(detail, /falls hardest at 2026-08-21/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
});

test('a canary model is never blamed', () => {
  const rows = switched({ newShare: 0.03 });
  assert.equal(Number(inputShareAfter(rows, NEW, 15).toFixed(2)), 0.03);
  const [state, detail] = classify(rows);
  assert.equal(state, 'new-model-marginal');
  assert.match(detail, /carries only 3% of input since/);
});

test('a window with no new model makes no claim', () => {
  const rows = Array.from({ length: 31 },
    (_, p) => day(p, p < 15 ? 0.70 : 0.10, { [OLD]: 40000000 }));
  assert.equal(arrivalPositions(rows).size, 0);
  const [state, detail] = classify(rows);
  assert.equal(state, 'no-new-model');
  assert.match(detail, /already present on day one/);
  assert.match(handoff(state), /cache-invalidated-by-changing-prefix/);
});

test('a share that holds across the switch is steady', () => {
  const [state, detail] = classify(switched({ cold: 0.70, after: 0.70 }));
  assert.equal(state, 'steady');
  assert.match(detail, /held at 70% against 70% before/);
});

test('a recovery after the step is only suggestive', () => {
  const rows = switched();
  rows[28].share = 0.90;
  const [state, detail] = classify(rows);
  assert.equal(state, 'partial-recovery');
  assert.match(detail, /recovered above the pre-switch floor/);
  assert.equal(sustained(rows.map((r) => r.share), 15), false);
});

test('the floors explain the step without making it', () => {
  assert.equal(cacheMinimum(NEW), 4096);
  assert.equal(cacheMinimum(OLD), 512);
  const note = floorNote(OLD, NEW);
  assert.match(note, /needs 4096 tokens/);
  assert.match(note, /prompt-below-model-cache-minimum/);
  assert.match(floorNote('claude-haiku-4-5', 'claude-opus-5'), /does not explain this/);
  assert.equal(floorNote(OLD, 'gpt-5.6'), '');
  assert.ok(repairLines(OLD, NEW).some((l) => l.includes('thinking')));
});

test('the report is folded into days and models', () => {
  const buckets = Array.from({ length: 31 }, (_, position) => {
    const model = position < 15 ? OLD : NEW;
    const share = position < 15 ? 0.70 : (position === 15 ? 0.20 : 0.10);
    const total = 40000000;
    const reads = Math.trunc(total * share);
    return { starting_at: `2026-08-${String(position + 1).padStart(2, '0')}T00:00:00Z`,
             results: [{ model, uncached_input_tokens: total - reads,
                         cache_read_input_tokens: reads,
                         cache_creation: { ephemeral_5m_input_tokens: 0,
                                           ephemeral_1h_input_tokens: 0 } }] };
  });
  const rows = dailyRows(buckets);
  assert.equal(rows.length, 31);
  assert.deepEqual(rows.map((r) => r.position), Array.from({ length: 31 }, (_, i) => i));
  assert.equal(Number(rows[0].share.toFixed(2)), 0.70);
  assert.equal(previousModel(rows, 15), OLD);
  assert.equal(classify(rows)[0], 'collapsed-after-model-change');
});

test('thin and unreadable windows produce no verdict', () => {
  const thin = Array.from({ length: 5 }, (_, p) => day(p, 0.5, { [OLD]: 1000 }));
  assert.equal(classify(thin)[0], 'too-few-days');
  assert.equal(classify([])[0], 'too-few-days');
  assert.equal(classify(null)[0], 'too-few-days');
  assert.deepEqual(stepAt([0.1, 0.2], 1), [null, null, null]);
  assert.deepEqual(bestSplit([0.1, 0.2]), [null, null]);
  assert.equal(sustained([], 3), false);
  assert.equal(inputShareAfter([], OLD, 0), null);
  assert.equal(previousModel([], 3), null);
  assert.equal(dayKey('nonsense'), null);
  assert.deepEqual(dailyRows([{ starting_at: 'bad', results: [] }]), []);
});
