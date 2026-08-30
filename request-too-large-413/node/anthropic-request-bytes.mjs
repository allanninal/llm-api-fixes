/**
 * Measure a Claude request in bytes against the 32 MB ceiling.
 *
 * Read only. One GET for the model object, and one optional call to
 * /v1/messages/count_tokens, which is free, creates no object, generates no
 * completion and is not billed. It is used purely as an oracle: it shares the
 * same 32 MB ceiling, so its status code answers the byte question at no cost.
 * The token number it returns is deliberately never read.
 *
 * /v1/messages is never called and nothing is uploaded.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const MB = 1024 * 1024;

const CEILINGS = {
  messages: 32 * MB,
  count_tokens: 32 * MB,
  batches: 256 * MB,
  files: 500 * MB,
};

const SAMPLING_ONLY = new Set(['max_tokens', 'stream', 'temperature', 'top_p',
  'top_k', 'stop_sequences', 'metadata', 'service_tier']);

const FINDINGS = new Set(['over-byte-ceiling', 'near-byte-ceiling',
  'over-content-cap', 'base64-has-newlines']);

/** The size of the JSON that actually goes on the wire. Pure. */
export function serializedBytes(body, escapeNonAscii = false) {
  let text = JSON.stringify(body);
  if (text === undefined) text = 'null';
  if (escapeNonAscii) {
    text = text.replace(/[\u0080-\uffff]/g, (ch) =>
      '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0'));
  }
  return Buffer.byteLength(text, 'utf8');
}

/** Bytes as a short readable string. Pure. Binary units throughout. */
export function human(size) {
  const n = Number(size || 0);
  if (n < 1024) return `${Math.trunc(n)} B`;
  if (n < MB) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / MB).toFixed(1)} MB`;
}

/**
 * How large a file becomes once base64 encoded. Pure.
 * Three bytes in, four characters out: exactly a third larger, which is why a
 * 24 MiB file lands on precisely the 32 MiB line.
 */
export function b64EncodedSize(rawBytes) {
  const raw = Math.max(0, Math.trunc(rawBytes || 0));
  return Math.floor((raw + 2) / 3) * 4;
}

/** The raw size behind a base64 string, without decoding it. Pure. */
export function b64DecodedSize(text) {
  const clean = String(text ?? '').replace(/\s+/g, '');
  if (!clean) return 0;
  const pad = (clean.match(/=/g) ?? []).length;
  return Math.floor(clean.length / 4) * 3 - pad;
}

/** The largest raw file that still fits inline under `ceiling`. Pure. */
export function inlineBudget(ceiling, envelope = 0) {
  const room = Math.max(0, Math.trunc(ceiling || 0) - Math.max(0, Math.trunc(envelope || 0)));
  return Math.floor(room / 4) * 3;
}

/** Every content block in a Messages body, flattened. Pure. */
export function contentBlocks(body) {
  const out = [];
  if (!body || typeof body !== 'object') return out;
  if (Array.isArray(body.system)) {
    out.push(...body.system.filter((b) => b && typeof b === 'object'));
  }
  for (const message of body.messages ?? []) {
    if (!message || typeof message !== 'object') continue;
    if (Array.isArray(message.content)) {
      out.push(...message.content.filter((b) => b && typeof b === 'object'));
    }
  }
  return out;
}

/** Images and documents in one request. Pure. A ceiling of its own. */
export function contentUnits(body) {
  return contentBlocks(body).filter((b) => b.type === 'image' || b.type === 'document').length;
}

/** Every inline base64 attachment, sized. Pure. */
export function base64Blobs(body) {
  const out = [];
  for (const block of contentBlocks(body)) {
    const source = block.source;
    if (!source || typeof source !== 'object' || source.type !== 'base64') continue;
    const data = source.data;
    if (typeof data !== 'string') continue;
    out.push({
      block: block.type,
      media_type: source.media_type,
      encoded: Buffer.byteLength(data, 'utf8'),
      raw: b64DecodedSize(data),
      newlines: data.includes('\n') || data.includes('\r'),
    });
  }
  return out;
}

/** How much larger the body gets if the client escapes non-ASCII. Pure. */
export function escapingPenalty(body) {
  const plain = serializedBytes(body, false);
  if (plain <= 0) return 1;
  return serializedBytes(body, true) / plain;
}

/** Images and PDF pages allowed in one request. Pure. null if unknown. */
export function contentCap(window) {
  if (!Number.isInteger(window) || window <= 0) return null;
  return window <= 200000 ? 100 : 600;
}

/** Classify one serialized body against one endpoint ceiling. Pure. */
export function sizeVerdict(endpoint, size, near = 0.8) {
  const ceiling = CEILINGS[endpoint];
  if (ceiling === undefined) {
    return ['endpoint-unknown',
      `no published byte ceiling for '${endpoint}', so there is nothing to ` +
      `compare ${human(size)} against`];
  }
  const shape = `${human(size)} of ${human(ceiling)} (${(size / ceiling * 100).toFixed(0)}%)`;
  if (size > ceiling) {
    return ['over-byte-ceiling',
      `${shape}. Cloudflare refuses this in front of the API with 413 ` +
      'request_too_large, so it never reaches Anthropic and never appears in ' +
      'any usage report.'];
  }
  if (size >= ceiling * near) {
    return ['near-byte-ceiling',
      `${shape}. Base64 costs a third on the way in, so one more attachment ` +
      'crosses the line.'];
  }
  return ['fits', `${shape}.`];
}

/** Classify the image and page count against the per request cap. Pure. */
export function contentVerdict(units, cap) {
  if (cap === null || cap === undefined) {
    return ['content-cap-unknown',
      `${units} image or document block(s), and no window on the model object ` +
      'to size the per request cap from'];
  }
  if (units > cap) {
    return ['over-content-cap',
      `${units} image or document block(s) against a cap of ${cap} for this ` +
      'model, which is refused whatever the payload weighs'];
  }
  return ['content-fits', `${units} image or document block(s) of a ${cap} cap`];
}

/** What the free counting endpoint's status code proves. Pure. Status only. */
export function probeState(status) {
  if (status === 413) {
    return ['confirmed-413',
      'the counting endpoint refused this body at the same 32 MB ceiling, so ' +
      'message creation refuses it too'];
  }
  if (status === 200) {
    return ['under-byte-ceiling',
      'the counting endpoint accepted the body, so it is inside the 32 MB ' +
      'ceiling for the endpoints that share it'];
  }
  return ['probe-inconclusive',
    `the counting endpoint answered ${status}, which is neither the 413 nor ` +
    'the 200 this probe reads'];
}

function headers(key) {
  return { 'x-api-key': key, 'anthropic-version': VERSION,
           'content-type': 'application/json' };
}

async function get(key, path) {
  const res = await fetch(API + path, { headers: headers(key) });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY has to be a workspace key`);
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

/** The one non-GET call, and it neither creates nor bills anything. */
async function probe(key, body) {
  const trimmed = Object.fromEntries(
    Object.entries(body ?? {}).filter(([k]) => !SAMPLING_ONLY.has(k)));
  const res = await fetch(`${API}/messages/count_tokens`, {
    method: 'POST',  // count_tokens creates nothing and bills nothing
    headers: headers(key),
    body: JSON.stringify(trimmed),
  });
  return res.status;
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key');
    process.exitCode = 2;
    return;
  }
  const paths = process.argv.slice(2).filter((a) => !a.startsWith('--'));
  if (paths.length === 0) {
    console.error('pass one or more payload JSON files');
    process.exitCode = 2;
    return;
  }
  const endpoint = (process.env.ENDPOINT || "https://example.com") ?? 'messages';
  const near = Number((process.env.NEAR || "dummy-near") ?? 0.8);
  const noProbe = (process.env.NO_PROBE || "dummy-no-probe") === '1';
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const windows = new Map();
  let checked = 0;
  let bad = 0;

  for (const path of paths) {
    const body = JSON.parse(await readFile(path, 'utf8'));
    checked += 1;

    const size = serializedBytes(body);
    const [state, detail] = sizeVerdict(endpoint, size, near);
    const line = `${state.padEnd(20)} ${path.padEnd(30)} ${detail}`;
    if (FINDINGS.has(state)) { bad += 1; console.warn(line); }
    else if (state === 'endpoint-unknown') console.warn(line);
    else if (showAll) console.log(line);

    const blobs = base64Blobs(body);
    if (blobs.length) {
      const raw = blobs.reduce((s, b) => s + b.raw, 0);
      const encoded = blobs.reduce((s, b) => s + b.encoded, 0);
      console.log(`  base64: ${blobs.length} blob(s), ${human(raw)} raw inflated ` +
                  `to ${human(encoded)} encoded ` +
                  `(${raw ? (encoded / raw * 100).toFixed(0) : 0}%)`);
    }
    const broken = blobs.filter((b) => b.newlines);
    if (broken.length) {
      bad += 1;
      console.warn(`${'base64-has-newlines'.padEnd(20)} ${path.padEnd(30)} ` +
                   `${broken.length} inline blob(s) contain line breaks; inline ` +
                   'base64 has to be unbroken, and several encoders still wrap ' +
                   'at 76 characters by default');
    }

    const penalty = escapingPenalty(body);
    if (penalty > 1.05) {
      console.warn(`  a client escaping non-ASCII would send ` +
                   `${((penalty - 1) * 100).toFixed(0)}% more than measured here ` +
                   `(${human(Math.trunc(size * penalty))}), which is enough to ` +
                   'cross the ceiling on its own');
    }

    const model = String(body.model ?? '');
    let window = null;
    if (model) {
      if (!windows.has(model)) {
        windows.set(model, (await get(key, `/models/${model}`)).max_input_tokens ?? null);
      }
      window = windows.get(model);
    }
    const units = contentUnits(body);
    if (units) {
      const [cstate, cdetail] = contentVerdict(units, contentCap(window));
      const cline = `${cstate.padEnd(20)} ${path.padEnd(30)} ${cdetail}`;
      if (cstate === 'over-content-cap') { bad += 1; console.warn(cline); }
      else if (cstate === 'content-cap-unknown') console.warn(cline);
      else if (showAll) console.log(cline);
    }

    if (!noProbe) {
      const [pstate, pdetail] = probeState(await probe(key, body));
      console.log(`  probe: ${pstate}, ${pdetail}`);
    }

    if (state === 'over-byte-ceiling' || state === 'near-byte-ceiling') {
      const envelope = size - blobs.reduce((s, b) => s + b.encoded, 0);
      console.warn('  largest raw file that still fits inline on this endpoint: ' +
                   human(inlineBudget(CEILINGS[endpoint], envelope)));
      console.warn('  repair: upload the attachment once through the Files API ' +
                   '(500 MB) and reference it by file_id, which takes the bytes ' +
                   'out of every request rather than one. Or split the request. ' +
                   'Printed, not performed.');
    }
  }

  console.log(`${checked} payload(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
