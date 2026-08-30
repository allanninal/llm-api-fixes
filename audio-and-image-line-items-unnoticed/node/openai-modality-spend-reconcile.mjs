/**
 * Reconcile an OpenAI token dashboard against the whole bill.
 *
 * Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
 * organization admin key with read scopes.
 *
 * Costs is the only endpoint denominated in money. The per-modality usage
 * endpoints are denominated in characters, seconds, images, sessions and calls.
 */
const API = 'https://api.openai.com/v1';

// Every usage surface, with the field it is denominated in.
const SURFACES = [
  ['completions', '/organization/usage/completions', 'num_model_requests', 'requests'],
  ['embeddings', '/organization/usage/embeddings', 'input_tokens', 'tokens'],
  ['moderations', '/organization/usage/moderations', 'input_tokens', 'tokens'],
  ['audio_speeches', '/organization/usage/audio_speeches', 'characters', 'characters'],
  ['audio_transcriptions', '/organization/usage/audio_transcriptions', 'seconds', 'seconds'],
  ['images', '/organization/usage/images', 'images', 'images'],
  ['code_interpreter_sessions', '/organization/usage/code_interpreter_sessions',
    'num_sessions', 'sessions'],
  ['file_search_calls', '/organization/usage/file_search_calls', 'num_requests', 'calls'],
  ['web_search_calls', '/organization/usage/web_search_calls', 'num_requests', 'calls'],
];

// Matched in order. Audio, image and tool come before text because
// "gpt-image-1" and "gpt-4o-audio-preview" both contain a text-model substring.
const FAMILIES = [
  ['audio', ['audio', 'speech', 'transcription', 'whisper', 'tts', 'realtime']],
  ['image', ['image', 'dall-e']],
  ['tool', ['web search', 'web_search', 'file search', 'file_search',
            'code interpreter', 'code_interpreter', 'container']],
  ['embedding', ['embedding']],
  ['moderation', ['moderation']],
  ['text', ['input tokens', 'output tokens', 'cached input', 'cached_input',
            'gpt-', 'o1-', 'o3', 'o4-', 'chat']],
];

const MIXED_TOKEN_FIELDS = ['input_audio_tokens', 'output_audio_tokens',
                            'input_image_tokens', 'output_image_tokens'];

const FINDINGS = ['gap', 'unclassified-line-items'];

/**
 * Map a cost report line_item onto a modality family. Pure. "other" is
 * deliberately loud rather than a quiet bucket.
 */
export function family(lineItem) {
  const name = String(lineItem ?? '').trim().toLowerCase();
  if (!name) return 'other';
  for (const [label, markers] of FAMILIES) {
    if (markers.some((m) => name.includes(m))) return label;
  }
  return 'other';
}

/**
 * Split spend into what the dashboard covers and what it does not. Pure.
 * Amounts that will not parse count as unreadable rather than as zero, because
 * zero would shrink the gap.
 */
export function reconcile(items, covers) {
  const out = { total: 0, covered: 0, uncovered: 0, unreadable: 0,
                by_family: {}, rows: [] };
  const wanted = new Set([...covers].map((c) => String(c).trim().toLowerCase()));
  for (const [lineItem, amount, quantity, unit] of items) {
    const value = Number(amount);
    if (!Number.isFinite(value) || amount === null || amount === undefined
        || amount === '') {
      out.unreadable += 1;
      continue;
    }
    const label = family(lineItem);
    out.total += value;
    out.by_family[label] = (out.by_family[label] ?? 0) + value;
    if (wanted.has(label)) out.covered += value;
    else {
      out.uncovered += value;
      out.rows.push([label, String(lineItem), value, quantity, unit]);
    }
  }
  out.rows.sort((a, b) => b[2] - a[2]);
  return out;
}

/**
 * Is the remainder rounding or a hole? Pure. Returns [state, detail].
 * A gap made mostly of unclassifiable line items gets its own state, because
 * the repair is to read the strings rather than to add a known endpoint.
 */
export function verdict(recon, tolerance = 0.02) {
  const total = recon.total ?? 0;
  const uncovered = recon.uncovered ?? 0;
  if (total <= 0) {
    return ['no-spend', 'no spend in the window, so there is nothing to reconcile'];
  }

  const share = uncovered / total;
  const money = `$${total.toFixed(2)} total, $${uncovered.toFixed(2)} ` +
                `(${(share * 100).toFixed(1)}%) outside what the dashboard covers`;

  if (share < tolerance) {
    return ['reconciled', `${money}, inside the ${(tolerance * 100).toFixed(1)}% tolerance`];
  }

  // Derived from the uncovered rows rather than from by_family, because
  // by_family counts both sides and the question here is only about the half
  // the dashboard cannot render.
  const uncoveredByFamily = {};
  for (const [label, , value] of recon.rows ?? []) {
    uncoveredByFamily[label] = (uncoveredByFamily[label] ?? 0) + value;
  }

  const other = uncoveredByFamily.other ?? 0;
  if (uncovered > 0 && other / uncovered > 0.5) {
    return ['unclassified-line-items',
      `${money}, and most of it is on line items this script could not ` +
      'classify. Read the raw line_item strings before assuming which endpoint ' +
      'explains them.'];
  }

  let biggest = ['nothing', 0];
  for (const [label, value] of Object.entries(uncoveredByFamily)) {
    if (value > biggest[1]) biggest = [label, value];
  }
  return ['gap',
    `${money}. Largest uncovered family is ${biggest[0]} at $${biggest[1].toFixed(2)}.`];
}

/**
 * Non-zero audio and image token counts inside a completions result. Pure.
 * A dashboard summing input_tokens and output_tokens whole is mixing these in
 * with text tokens at the text price.
 */
export function hiddenTokenTypes(result) {
  const out = [];
  for (const field of MIXED_TOKEN_FIELDS) {
    const value = Number(result[field] ?? 0);
    if (Number.isFinite(value) && value !== 0) out.push([field, Math.trunc(value)]);
  }
  return out.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: OPENAI_ADMIN_KEY must be an organization ' +
                    'admin key, not a project key');
  }
  if (res.status === 403) {
    throw new Error('403 from OpenAI: the key is not authorised for /v1/organization');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function costItems(key, startTime) {
  const out = [];
  const page = await get(key, '/organization/costs',
    { start_time: startTime, limit: 31, group_by: 'line_item' });
  for (const bucket of page.data ?? []) {
    for (const result of bucket.results ?? []) {
      out.push([result.line_item, result.amount?.value, result.quantity,
                result.quantity_unit]);
    }
  }
  return out;
}

async function surfaceVolume(key, path, field, startTime, days) {
  let total = 0;
  const page = await get(key, path,
    { start_time: startTime, bucket_width: '1d', limit: days });
  for (const bucket of page.data ?? []) {
    for (const result of bucket.results ?? []) {
      const n = Number(result[field] ?? 0);
      if (Number.isFinite(n)) total += Math.trunc(n);
    }
  }
  return total;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read scopes)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const covers = ((process.env.COVERS || "dummy-covers") ?? 'text').split(',').filter((c) => c.trim());
  const tolerance = Number((process.env.TOLERANCE || "dummy-tolerance") ?? 0.02);

  const now = Math.floor(Date.now() / 1000);
  const start = now - days * 86400;

  const recon = reconcile(await costItems(key, start), covers);
  const [state, detail] = verdict(recon, tolerance);
  console.log(`${state.padEnd(24)} ${detail}`);

  for (const [label, lineItem, value, quantity, unit] of recon.rows.slice(0, 20)) {
    console.warn(`  uncovered  ${label.padEnd(10)} $${value.toFixed(2).padStart(9)}` +
                 `   ${String(lineItem).padEnd(28)} ${quantity ?? ''} ${unit ?? ''}`);
  }

  for (const [name, path, field, unit] of SURFACES) {
    const volume = await surfaceVolume(key, path, field, start, days);
    if (volume) console.log(`  volume     ${name.padEnd(28)} ${volume} ${unit}`);
  }

  if (recon.unreadable) {
    console.warn(`  ${recon.unreadable} cost row(s) had an unreadable amount and ` +
                 'were left out of both sides');
  }

  if (FINDINGS.includes(state)) {
    console.warn('  repair: drive the spend dashboard from /v1/organization/costs ' +
      'grouped by line_item, which is the only endpoint denominated in money, ' +
      'and use the per-modality usage endpoints to explain why a line moved');
    console.warn('  repair: inside completions, read input_text_tokens, ' +
      'input_audio_tokens and input_image_tokens separately instead of summing ' +
      'input_tokens whole');
    process.exitCode = 1;
    return;
  }
  process.exitCode = 0;
}

// Only run when invoked directly, so importing this from the test file does not
// run main(), fail on the missing key, and set an exit code that fails the suite.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
