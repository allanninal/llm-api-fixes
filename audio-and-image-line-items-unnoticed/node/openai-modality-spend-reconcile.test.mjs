import { test } from 'node:test';
import assert from 'node:assert/strict';
import { family, hiddenTokenTypes, reconcile, verdict }
  from './openai-modality-spend-reconcile.mjs';

/** [[line_item, amount, quantity, quantity_unit], ...] from the cost report. */
function items(rows) {
  return rows.map((r) => [r[0], r[1], r[2] ?? null, r[3] ?? null]);
}

test('the dashboard covers text and the bill does not stop there', () => {
  const recon = reconcile(items([
    ['gpt-5, input tokens', 9000.00],
    ['gpt-5, output tokens', 6487.43],
    ['Text-to-speech', 1802.40, 14209881, 'characters'],
    ['Web search', 784.00, 78400, 'requests'],
    ['Image generation', 328.28, 6120, 'images'],
  ]), ['text']);
  const [state, detail] = verdict(recon);
  assert.equal(state, 'gap');
  assert.match(detail, /18402.11 total/);
  assert.match(detail, /2914.68/);
  assert.equal(recon.rows[0][0], 'audio');
});

test('model names that look like text but are not', () => {
  assert.equal(family('gpt-image-1'), 'image');
  assert.equal(family('gpt-4o-audio-preview, input tokens'), 'audio');
  assert.equal(family('gpt-5, input tokens'), 'text');
  assert.equal(family('Code interpreter session'), 'tool');
  assert.equal(family('text-embedding-3-small'), 'embedding');
});

test('a small gap is rounding and a large one is not', () => {
  const small = reconcile(items([['gpt-5, input tokens', 1000.00],
                                 ['Moderations', 5.00]]), ['text']);
  assert.equal(verdict(small)[0], 'reconciled');
  assert.equal(verdict(small, 0.001)[0], 'gap');
});

test('line items nobody can classify are their own state', () => {
  const recon = reconcile(items([['gpt-5, input tokens', 500.00],
                                 ['Some New Surface We Shipped Tuesday', 400.00]]),
                          ['text']);
  const [state, detail] = verdict(recon);
  assert.equal(state, 'unclassified-line-items');
  assert.match(detail.toLowerCase(), /read the raw line_item strings/);
});

test('an unreadable amount is not counted as zero', () => {
  const recon = reconcile(items([['gpt-5, input tokens', 100.00],
                                 ['Text-to-speech', null],
                                 ['Web search', 'n/a']]), ['text']);
  assert.equal(recon.unreadable, 2);
  assert.equal(recon.total, 100.00);
  assert.equal(verdict(recon)[0], 'reconciled');
});

test('nothing to reconcile is not a finding', () => {
  assert.equal(verdict(reconcile([], ['text']))[0], 'no-spend');
});

test('multimodal tokens hide inside the completions result', () => {
  const result = {
    input_tokens: 100000, output_tokens: 8000, input_text_tokens: 60000,
    input_audio_tokens: 40000, output_audio_tokens: 3000, input_image_tokens: 0,
  };
  assert.deepEqual(hiddenTokenTypes(result),
                   [['input_audio_tokens', 40000], ['output_audio_tokens', 3000]]);
  assert.deepEqual(hiddenTokenTypes({ input_tokens: 100000 }), []);
});
