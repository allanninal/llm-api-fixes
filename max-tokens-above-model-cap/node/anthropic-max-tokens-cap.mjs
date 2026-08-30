/**
 * Compare each configured max_tokens against the model's own published cap.
 *
 * Read only. GET requests and nothing else: give this a workspace API key. No
 * payload is ever sent, no tokens are counted, and /v1/messages is never
 * called. The repair is printed.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const BATCH_300K_BETA = 'output-300k-2026-03-24';
const BATCH_MAX_TOKENS = 300000;
const LONG_CONTEXT_WINDOW = 1000000;

const FINDINGS = new Set(['above-cap', 'below-minimum', 'cap-unknown', 'model-not-found']);

/** Read a NAME=MODEL:MAX_TOKENS argument. Pure. [name, entry] or null. */
export function parsePath(spec) {
  const text = String(spec ?? '').trim();
  const eq = text.indexOf('=');
  if (eq < 0) return null;
  const name = text.slice(0, eq).trim();
  const rest = text.slice(eq + 1);
  const colon = rest.lastIndexOf(':');
  if (colon < 0) return null;
  const model = rest.slice(0, colon).trim();
  const value = rest.slice(colon + 1).trim();
  if (!name || !model || !/^-?[0-9]+$/.test(value)) return null;
  return [name, { model, max_tokens: Number(value), endpoint: 'messages' }];
}

/** The model object's own max_tokens field. Pure. null if absent. */
export function syncCap(modelObj) {
  if (!modelObj || typeof modelObj !== 'object') return null;
  const value = modelObj.max_tokens;
  return Number.isInteger(value) && value > 0 ? value : null;
}

/** max_input_tokens off a model object. Pure. Sizes the batch ceiling only. */
export function windowOf(modelObj) {
  if (!modelObj || typeof modelObj !== 'object') return null;
  const value = modelObj.max_input_tokens;
  return Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * The legal ceiling for max_tokens on one model at one endpoint. Pure.
 * The ceiling belongs to the pair: a batch path with the output-300k header on
 * a 1M-context model gets the higher number and nothing else does.
 */
export function effectiveCap(modelObj, endpoint = 'messages', betas = []) {
  const cap = syncCap(modelObj);
  if (cap === null) return [null, 'the model object carried no max_tokens field'];
  if (String(endpoint) === 'batches' && new Set(betas ?? []).has(BATCH_300K_BETA)) {
    const window = windowOf(modelObj);
    if (window !== null && window >= LONG_CONTEXT_WINDOW) {
      return [BATCH_MAX_TOKENS, `the Batch API with ${BATCH_300K_BETA}`];
    }
    return [cap, 'the model object; the 300K batch ceiling needs a 1M context model'];
  }
  return [cap, 'the model object'];
}

/** Classify one configured value against one cap. Pure. [state, detail]. */
export function verdict(configured, cap) {
  const value = Math.trunc(configured || 0);
  if (value < 1) {
    return ['below-minimum',
      `max_tokens is ${value}, and the minimum accepted value is 1`];
  }
  if (cap === null || cap === undefined) {
    return ['cap-unknown',
      `max_tokens is ${value} and no ceiling could be read for this model and endpoint`];
  }
  if (value > cap) {
    return ['above-cap',
      `max_tokens is ${value} against a cap of ${cap}, which is a 400 ` +
      `invalid_request_error on every call, ${value - cap} over`];
  }
  if (value === cap) {
    return ['at-cap',
      `max_tokens is ${value}, exactly the cap, so any move to a smaller model ` +
      'breaks this path'];
  }
  return ['within-cap',
    `max_tokens is ${value} of a ${cap} cap (${(value / cap * 100).toFixed(0)}%)`];
}

/**
 * One configured value reused across models with different ceilings. Pure.
 * rows: [[name, modelId, configured, cap]]. Returns [[value, [modelIds]]].
 */
export function tierSpans(rows) {
  const byValue = new Map();
  for (const [name, model, configured, cap] of rows ?? []) {
    const value = Math.trunc(configured || 0);
    if (!byValue.has(value)) byValue.set(value, []);
    byValue.get(value).push([name, model, cap]);
  }
  const out = [];
  for (const value of [...byValue.keys()].sort((a, b) => a - b)) {
    const models = [...new Set(byValue.get(value).map(([, m]) => m))].sort();
    if (models.length < 2) continue;
    out.push([value, models]);
  }
  return out;
}

async function getModel(key, modelId) {
  const res = await fetch(`${API}/models/${modelId}`, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 404) return null;
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY has to be a workspace key`);
  }
  if (!res.ok) throw new Error(`${res.status} from /models/${modelId}`);
  return res.json();
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key');
    process.exitCode = 2;
    return;
  }
  const paths = {};
  if ((process.env.CONFIG || "dummy-config")) Object.assign(paths, JSON.parse(await readFile((process.env.CONFIG || "dummy-config"), 'utf8')));
  for (const spec of process.argv.slice(2).filter((a) => !a.startsWith('--'))) {
    const parsed = parsePath(spec);
    if (!parsed) {
      console.error(`cannot read '${spec}', expected NAME=MODEL:MAX_TOKENS`);
      process.exitCode = 2;
      return;
    }
    paths[parsed[0]] = parsed[1];
  }
  if (Object.keys(paths).length === 0) {
    console.error('set CONFIG to a JSON file, or pass NAME=MODEL:MAX_TOKENS arguments');
    process.exitCode = 2;
    return;
  }
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const models = new Map();
  const rows = [];
  let bad = 0;

  for (const name of Object.keys(paths).sort()) {
    const entry = paths[name] ?? {};
    const modelId = String(entry.model ?? '');
    const endpoint = entry.endpoint ?? 'messages';
    const betas = entry.betas ?? [];

    if (!models.has(modelId)) models.set(modelId, await getModel(key, modelId));
    const modelObj = models.get(modelId);
    if (modelObj === null) {
      bad += 1;
      console.warn(`${'model-not-found'.padEnd(14)} ${name.padEnd(16)} ` +
                   `${modelId.padEnd(28)} the model id is not in the live list at ` +
                   'all, which is a retirement or a typo rather than a max_tokens problem');
      continue;
    }

    const [cap, source] = effectiveCap(modelObj, endpoint, betas);
    const [state, detail] = verdict(entry.max_tokens, cap);
    rows.push([name, modelId, Math.trunc(entry.max_tokens || 0), cap]);

    const line = `${state.padEnd(14)} ${name.padEnd(16)} ${modelId.padEnd(28)} ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      console.warn(`  ceiling read from ${source}`);
    } else if (state === 'at-cap') {
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  for (const [value, shared] of tierSpans(rows)) {
    const caps = rows.filter(([, , configured, cap]) => configured === value && cap !== null)
      .map(([, , , cap]) => cap);
    const note = `shared value ${value} is configured on ${shared.length} model(s): ` +
                 shared.join(', ');
    if (caps.length && Math.min(...caps) < value) {
      bad += 1;
      console.warn(`${'spans-tiers'.padEnd(14)} ${note}, and the smallest cap ` +
                   `among them is ${Math.min(...caps)}`);
    } else {
      console.log(`  ${note}, so the effective ceiling is the smallest of their ` +
                  'caps whether or not anything says so');
    }
  }

  if (bad) {
    console.warn('  repair: set each path\'s max_tokens from the cap the Models API ' +
                 'reports for its own model, not from a shared constant and not from ' +
                 'the docs table, which lags. Note that maxing it out trades a 400 for ' +
                 'truncated answers and long non-streaming requests. Printed, not applied.');
  }

  console.log(`${Object.keys(paths).length} path(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
