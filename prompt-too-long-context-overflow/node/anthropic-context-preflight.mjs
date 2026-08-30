/**
 * Pre-flight a Claude payload against the model's context window.
 *
 * Read only, with one deliberate exception. Nothing here creates a completion:
 * the payload goes to /v1/messages/count_tokens, which is free, generates no
 * output, creates no object and bills nothing. Everything else is a GET, and
 * /v1/messages is never called.
 *
 * The repair is printed, never applied.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const SAMPLING_ONLY = new Set(['max_tokens', 'stream', 'temperature', 'top_p',
  'top_k', 'stop_sequences', 'metadata', 'service_tier']);

const OVERFLOW_STOP = 'model_context_window_exceeded';
const TOO_LONG = 'prompt is too long';

const FINDINGS = new Set(['input-over-window', 'budget-over-window', 'window-tight']);

/** The subset of a Messages body the counting endpoint accepts. Pure. */
export function countBody(body) {
  if (!body || typeof body !== 'object') return {};
  return Object.fromEntries(
    Object.entries(body).filter(([k]) => !SAMPLING_ONLY.has(k)));
}

/**
 * max_input_tokens off a model object, or null. Pure.
 * Null is not a large window: a ceiling a gateway dropped has to stay missing
 * rather than defaulting to something every payload fits under.
 */
export function windowOf(modelObj) {
  if (!modelObj || typeof modelObj !== 'object') return null;
  const value = modelObj.max_input_tokens;
  return Number.isInteger(value) && value > 0 ? value : null;
}

/** What one request reserves in the window: input plus room for output. Pure. */
export function budget(countedInput, maxTokens) {
  return Math.trunc(countedInput || 0) + Math.max(0, Math.trunc(maxTokens || 0));
}

/** Classify one payload against one model's window. Pure. [state, detail]. */
export function verdict(countedInput, maxTokens, window, tight = 0.9) {
  const input = Math.trunc(countedInput || 0);
  const reserved = budget(input, maxTokens);

  if (window === null || window === undefined) {
    return ['window-unknown',
      `${input} input token(s) counted, and the model object carried no ` +
      'max_input_tokens, so there is no ceiling to compare against'];
  }

  const room = Math.max(0, Math.trunc(maxTokens || 0));
  const shape = `${input} input + ${room} max_tokens = ${reserved} of a ` +
                `${window} token window`;

  if (input > window) {
    return ['input-over-window',
      `${shape}. The input alone is over the window, so this 400s with prompt ` +
      'is too long on every model, before max_tokens is even considered.'];
  }
  if (reserved > window) {
    return ['budget-over-window',
      `${shape}. The input fits and the reservation does not. On Claude 4.5 ` +
      `and newer that returns 200 with stop_reason ${OVERFLOW_STOP}, which a ` +
      'client checking only for end_turn files as a complete answer.'];
  }

  const share = reserved / window;
  const pct = (share * 100).toFixed(0);
  if (share >= tight) {
    return ['window-tight',
      `${shape} (${pct}%). It fits today and one longer turn ends that.`];
  }
  return ['fits', `${shape} (${pct}%).`];
}

/** How many more turns of `perTurn` tokens fit. Pure. null if unanswerable. */
export function turnsRemaining(countedInput, maxTokens, window, perTurn) {
  if (!window || !perTurn || perTurn <= 0) return null;
  const room = window - budget(countedInput, maxTokens);
  return Math.max(0, Math.floor(room / perTurn));
}

/**
 * Find window overflows in a batch results stream. Pure.
 * Both shapes: a 200 carrying the overflow stop reason, and an errored result
 * whose message says the prompt is too long. Keyed by custom_id, never by
 * position, because results arrive in any order.
 */
export function batchOverflows(lines) {
  const out = {};
  for (const line of lines ?? []) {
    let record = line;
    if (typeof record === 'string') {
      const text = record.trim();
      if (!text) continue;
      try { record = JSON.parse(text); } catch { continue; }
    }
    if (!record || typeof record !== 'object') continue;

    const customId = record.custom_id;
    const result = record.result ?? {};
    const message = result.message ?? {};
    if (message.stop_reason === OVERFLOW_STOP) {
      out[customId] = 'truncated-with-200';
      continue;
    }
    const error = result.error ?? {};
    if (String(error.message ?? '').toLowerCase().includes(TOO_LONG)) {
      out[customId] = 'rejected-with-400';
    }
  }
  return out;
}

function headers(key) {
  return { 'x-api-key': key, 'anthropic-version': VERSION,
           'content-type': 'application/json' };
}

async function get(key, path) {
  const res = await fetch(API + path, { headers: headers(key) });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY has to be ` +
                    'a workspace key that can reach /v1/models');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

/**
 * The one call here that is not a GET, and not a write either: the counting
 * endpoint creates nothing, generates nothing and is not billed.
 */
async function countTokens(key, body) {
  const res = await fetch(`${API}/messages/count_tokens`, {
    method: 'POST',  // count_tokens creates nothing and bills nothing
    headers: headers(key),
    body: JSON.stringify(countBody(body)),
  });
  if (res.status === 413) {
    throw new Error('413 from the counting endpoint: this body is over the ' +
                    '32 MB request ceiling, which is a byte problem rather ' +
                    'than a token one');
  }
  if (!res.ok) throw new Error(`${res.status} from /messages/count_tokens`);
  return Math.trunc((await res.json())?.input_tokens ?? 0);
}

async function batchResults(key, batchId) {
  const res = await fetch(`${API}/messages/batches/${batchId}/results`,
                          { headers: headers(key) });
  if (!res.ok) throw new Error(`${res.status} from batch ${batchId} results`);
  return (await res.text()).split('\n');
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key');
    process.exitCode = 2;
    return;
  }
  const paths = process.argv.slice(2).filter((a) => !a.startsWith('--'));
  const batchIds = ((process.env.BATCH_IDS || "dummy-batch-ids") ?? '').split(',')
    .map((s) => s.trim()).filter(Boolean);
  if (paths.length === 0 && batchIds.length === 0) {
    console.error('pass one or more payload JSON files, or set BATCH_IDS');
    process.exitCode = 2;
    return;
  }
  const perTurn = Math.trunc(Number((process.env.PER_TURN || "dummy-per-turn") ?? 0));
  const tight = Number((process.env.TIGHT || "dummy-tight") ?? 0.9);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const windows = new Map();
  let checked = 0;
  let bad = 0;

  for (const path of paths) {
    const body = JSON.parse(await readFile(path, 'utf8'));
    const model = String(body.model ?? '');
    if (!model) {
      bad += 1;
      console.warn(`${'no-model'.padEnd(20)} ${path.padEnd(30)} no model field, ` +
                   'so there is no window to check it against');
      continue;
    }
    if (!windows.has(model)) windows.set(model, windowOf(await get(key, `/models/${model}`)));

    const counted = await countTokens(key, body);
    const [state, detail] = verdict(counted, body.max_tokens, windows.get(model), tight);
    checked += 1;
    const line = `${state.padEnd(20)} ${path.padEnd(30)} ${detail}`;
    if (FINDINGS.has(state) || state === 'window-unknown') {
      if (FINDINGS.has(state)) bad += 1;
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }

    const left = turnsRemaining(counted, body.max_tokens, windows.get(model), perTurn);
    if (left !== null) console.log(`  room for ${left} more turn(s) at ${perTurn} tokens each`);
    if (FINDINGS.has(state)) {
      console.warn('  repair: server side compaction (compact-2026-01-12) for long ' +
                   'conversations, context editing (clear_tool_uses_20250919 / ' +
                   'clear_thinking_20251015) for agent loops, or the tool search ' +
                   'tool so tool definitions stop being resident on every turn');
      console.warn('  repair: caching does not help here. Cached tokens still ' +
                   'occupy the window; they only cost less.');
    }
  }

  for (const batchId of batchIds) {
    const found = batchOverflows(await batchResults(key, batchId));
    const ids = Object.keys(found).sort();
    checked += ids.length;
    for (const customId of ids) {
      bad += 1;
      console.warn(`${found[customId].padEnd(20)} ${String(customId).padEnd(30)} ` +
                   `in batch ${batchId}`);
    }
  }

  console.log(`${checked} payload(s) and batch result(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
