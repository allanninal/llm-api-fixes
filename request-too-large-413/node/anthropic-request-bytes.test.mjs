import { test } from 'node:test';
import assert from 'node:assert/strict';
import { b64DecodedSize, b64EncodedSize, base64Blobs, contentCap, contentUnits,
         contentVerdict, escapingPenalty, human, inlineBudget, probeState,
         serializedBytes, sizeVerdict } from './anthropic-request-bytes.mjs';

const MB = 1024 * 1024;

test('a 24mb file lands exactly on the 32mb line', () => {
  assert.equal(b64EncodedSize(24 * MB), 32 * MB);
  assert.equal(b64EncodedSize(24 * MB), 33554432);
  assert.equal(sizeVerdict('messages', 32 * MB)[0], 'near-byte-ceiling');
  const [state, detail] = sizeVerdict('messages', 32 * MB + 4096);
  assert.equal(state, 'over-byte-ceiling');
  assert.match(detail, /Cloudflare/);
  assert.match(detail, /never appears in any usage report/);
  assert.equal(inlineBudget(32 * MB, 4096), 24 * MB - 3072);
});

test('the image cap is a separate ceiling from the bytes', () => {
  assert.equal(contentCap(200000), 100);
  assert.equal(contentCap(1000000), 600);
  assert.equal(contentCap(null), null);
  assert.equal(contentVerdict(300, 100)[0], 'over-content-cap');
  assert.equal(contentVerdict(300, 600)[0], 'content-fits');
  assert.equal(contentVerdict(300, null)[0], 'content-cap-unknown');
  assert.equal(sizeVerdict('messages', 2 * MB)[0], 'fits');
});

test('the ceiling depends on the endpoint not on the body', () => {
  const size = 200 * MB;
  assert.equal(sizeVerdict('messages', size)[0], 'over-byte-ceiling');
  assert.equal(sizeVerdict('batches', size)[0], 'fits');
  assert.equal(sizeVerdict('files', size)[0], 'fits');
  assert.equal(sizeVerdict('responses', size)[0], 'endpoint-unknown');
});

test('blobs are sized without decoding them', () => {
  const data = 'QUJDREVGR0g=';  // eight raw bytes, twelve encoded characters
  const body = { model: 'claude-sonnet-5', messages: [{ role: 'user', content: [
    { type: 'text', text: 'read this' },
    { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data } },
  ] }] };
  const blobs = base64Blobs(body);
  assert.equal(blobs.length, 1);
  assert.equal(blobs[0].media_type, 'application/pdf');
  assert.equal(blobs[0].encoded, 12);
  assert.equal(blobs[0].raw, b64DecodedSize(data));
  assert.equal(blobs[0].raw, 8);
  assert.equal(blobs[0].newlines, false);
  assert.equal(contentUnits(body), 1);
});

test('line wrapped base64 is its own rejection', () => {
  const body = { messages: [{ role: 'user', content: [
    { type: 'image', source: { type: 'base64', media_type: 'image/png',
                               data: 'QUJDREVG\nR0g=' } }] }] };
  assert.equal(base64Blobs(body)[0].newlines, true);
  assert.equal(base64Blobs(body)[0].raw, 8);
});

test('a client that escapes non ascii sends more than you measured', () => {
  const body = { messages: [{ role: 'user', content: '\u3053\u3093\u306b\u3061\u306f'.repeat(100) }] };
  const plain = serializedBytes(body);
  const escaped = serializedBytes(body, true);
  assert.ok(escaped > plain);
  assert.equal(escapingPenalty(body), escaped / plain);
  assert.ok(escapingPenalty(body) > 1.9);
  assert.equal(escapingPenalty({ messages: [{ role: 'user', content: 'hello' }] }), 1);
});

test('the probe is read as a status code not as a token count', () => {
  assert.equal(probeState(413)[0], 'confirmed-413');
  assert.equal(probeState(200)[0], 'under-byte-ceiling');
  assert.equal(probeState(400)[0], 'probe-inconclusive');
  assert.equal(probeState(429)[0], 'probe-inconclusive');
});

test('sizes are printed in binary units', () => {
  assert.equal(human(0), '0 B');
  assert.equal(human(1023), '1023 B');
  assert.equal(human(1024), '1.0 KB');
  assert.equal(human(32 * MB), '32.0 MB');
});
