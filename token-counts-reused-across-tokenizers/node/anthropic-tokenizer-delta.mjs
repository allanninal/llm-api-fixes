/**
 * Measure the token delta between two Claude models on one identical body.
 *
 * Claude 4.7 and later use a newer tokenizer that produces roughly 30 percent
 * more tokens for the same text; the exact increase depends on the content.
 *
 * The only non-GET request in this section: POST /v1/messages/count_tokens,
 * which is free, creates no message and generates nothing.
 *
 * The two calls may differ only in the model field, which is asserted before
 * either one is sent. A budgeting reading, never a ceiling one.
 */
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const COUNT_TOKENS_URL = 'https://api.anthropic.com/v1/messages/count_tokens';

const GENERATION_ONLY = new Set(['max_tokens', 'temperature', 'top_p', 'top_k',
  'stream', 'stop_sequences', 'service_tier', 'metadata']);

export const TOLERANCE = 0.02;

export const MEASURED =
  'measured: two input_tokens values from count_tokens on identical bodies';

const FINDINGS = new Set(['tokenizer-delta', 'count-failed', 'bodies-differ']);

/** A counting body from a Messages body. Pure. Generation fields removed. */
export function countBody(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return {};
  const out = {};
  for (const [k, v] of Object.entries(body)) {
    if (!GENERATION_ONLY.has(k)) out[k] = v;
  }
  return out;
}

/** The same body under a different model id. Pure. One key changes. */
export function swapModel(body, model) {
  return { ...(body ?? {}), model: String(model) };
}

const canonical = (value) => {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = canonical(value[key]);
    return out;
  }
  return value;
};

/** True when the only difference is the model field. Pure. */
export function sameApartFromModel(left, right) {
  const strip = (obj) => {
    const { model, ...rest } = obj ?? {};
    return JSON.stringify(canonical(rest));
  };
  return strip(left) === strip(right);
}

/** target / base. Pure. Null when the base count is unusable. */
export function ratio(base, target) {
  const b = Number(base);
  const t = Number(target);
  if (!Number.isFinite(b) || !Number.isFinite(t) || b <= 0) return null;
  return t / b;
}

/** Token-weighted ratio across the sample. Pure. Null when nothing counted. */
export function workloadRatio(rows) {
  let base = 0;
  let target = 0;
  for (const row of rows ?? []) {
    base += Number(row?.baseTokens ?? 0) || 0;
    target += Number(row?.targetTokens ?? 0) || 0;
  }
  return ratio(base, target);
}

/** [[name, old, new]] for each declared constant. Pure. Sorted by name. */
export function rebaseline(budgets, r) {
  if (!r) return [];
  return Object.keys(budgets ?? {}).sort()
    .map((name) => [name, Math.trunc(budgets[name]),
                    Math.round(Math.trunc(budgets[name]) * r)]);
}

/** {name: tokens} from name=tokens pairs. Pure. Bad pairs are dropped. */
export function parseBudgets(raw) {
  const out = {};
  for (const item of raw ?? []) {
    for (const part of String(item).split(',')) {
      const at = part.indexOf('=');
      if (at < 0) continue;
      const name = part.slice(0, at).trim();
      const tokens = Number.parseInt(part.slice(at + 1).trim().replace(/_/g, ''), 10);
      if (name && Number.isFinite(tokens) && tokens > 0) out[name] = tokens;
    }
  }
  return out;
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(rows, baseModel, targetModel) {
  const list = [...(rows ?? [])];
  if (!list.length) {
    return ['no-bodies', 'no bodies were counted, so there is nothing to compare'];
  }
  const failed = list.filter((r) => r?.error);
  if (failed.length === list.length) {
    return ['count-failed', `every count failed: ${failed[0].error}`];
  }
  if (list.some((r) => r?.mismatch)) {
    return ['bodies-differ',
      'at least one pair of bodies differed by more than the model field, so no '
      + 'ratio was taken for it'];
  }
  const r = workloadRatio(list);
  if (r === null) return ['count-failed', 'no usable input_tokens came back'];
  const counted = list.filter((x) => !x?.error);
  if (Math.abs(r - 1) < TOLERANCE) {
    return ['counts-agree',
      `${baseModel} and ${targetModel} count this workload within `
      + `${Math.trunc(TOLERANCE * 100)}% of each other, so they share a `
      + 'tokenizer and no constant needs re-baselining'];
  }
  return ['tokenizer-delta',
    `the workload counts ${r.toFixed(3)}x more tokens on ${targetModel}, `
    + `measured over ${counted.length} body/bodies`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, r) {
  if (state === 'tokenizer-delta') {
    const lines = ['re-baseline every constant above, and key any stored token '
      + 'count by model as well as by text. A count with no model attached is '
      + 'wrong for one of the two models and you cannot tell which.'];
    if (r && r > 1) {
      lines.push('expect input spend on this workload to move by about '
        + `${Math.round((r - 1) * 100)}% at flat traffic, since billing follows `
        + 'the count the model actually consumed.');
      lines.push('prompts assembled to a fixed token budget now carry less '
        + 'content than they did. Check retrieval quality and any compaction '
        + 'threshold before blaming the model.');
    }
    return lines;
  }
  if (state === 'bodies-differ') {
    return ['the two bodies differed by more than the model field, so the ratio '
      + 'would have measured the harness. Count one body, swap only model, and '
      + 'send it twice.'];
  }
  if (state === 'count-failed') {
    return ['read the error text above. A 400 naming the model is an id this '
      + 'account cannot reach; a 413 is the 32 MB byte ceiling, which is a '
      + 'different note.'];
  }
  if (state === 'counts-agree') {
    return ['nothing to change here. Both ids are on the same tokenizer, so '
      + 'counts measured on one transfer to the other.'];
  }
  return [];
}

async function countTokens(body, key) {
  let res;
  try {
    res = await fetch(COUNT_TOKENS_URL, {
      method: 'POST', // /v1/messages/count_tokens: free, creates and bills nothing
      headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01',
                 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    return [null, `request failed: ${err.message}`];
  }
  if (res.status !== 200) {
    let detail = '';
    try { detail = String((await res.json())?.error?.message ?? ''); } catch { detail = ''; }
    return [null, `HTTP ${res.status} ${detail}`];
  }
  try {
    const parsed = await res.json();
    return [Math.trunc(Number(parsed.input_tokens)), null];
  } catch {
    return [null, 'no input_tokens in the response'];
  }
}

function args(argv) {
  const out = { body: [], budget: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === '--from') out.from = argv[i += 1];
    else if (flag === '--to') out.to = argv[i += 1];
    else if (flag === '--body') out.body.push(argv[i += 1]);
    else if (flag === '--budget') out.budget.push(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key. It is used only for '
      + 'POST /v1/messages/count_tokens, which is free and creates nothing');
    process.exitCode = 2;
    return;
  }
  if (!opts.from || !opts.to || !opts.body.length) {
    console.error('usage: --from <model> --to <model> --body <file.json> '
      + '[--budget name=tokens]');
    process.exitCode = 2;
    return;
  }

  const budgets = parseBudgets([...opts.budget,
                                (process.env.ANTHROPIC_TOKEN_BUDGETS || "dummy-anthropic-token-budgets") ?? '']);
  const rows = [];
  for (const file of opts.body) {
    const name = path.basename(file);
    let raw;
    try {
      raw = JSON.parse(await readFile(file, 'utf8'));
    } catch (err) {
      rows.push({ name, error: `unreadable: ${err.message}` });
      console.log(`${name.padEnd(24)} unreadable: ${err.message}`);
      continue;
    }
    const baseBody = swapModel(countBody(raw), opts.from);
    const targetBody = swapModel(countBody(raw), opts.to);
    if (!sameApartFromModel(baseBody, targetBody)) {
      rows.push({ name, mismatch: true });
      console.log(`${name.padEnd(24)} the two bodies differ by more than model`);
      continue;
    }
    const [baseTokens, baseErr] = await countTokens(baseBody, key);
    const [targetTokens, targetErr] = await countTokens(targetBody, key);
    const err = baseErr || targetErr;
    if (err) {
      rows.push({ name, error: err });
      console.log(`${name.padEnd(24)} ${err}`);
      continue;
    }
    const r = ratio(baseTokens, targetTokens);
    rows.push({ name, baseTokens, targetTokens, ratio: r });
    console.log(`${name.padEnd(24)} ${opts.from} ${baseTokens} -> ${opts.to} `
      + `${targetTokens}   x${(r ?? 0).toFixed(3)}`);
  }

  const [state, detail] = verdict(rows, opts.from, opts.to);
  const r = workloadRatio(rows);
  console.log(`${state.padEnd(20)} ${detail}`);
  if (state === 'tokenizer-delta' || state === 'counts-agree') {
    console.log(`  ${MEASURED}`);
    console.log(`  inferred: that this ratio holds for traffic these `
      + `${rows.filter((x) => x.ratio).length} bodies represent`);
  }
  for (const [name, old, next] of rebaseline(budgets, r)) {
    console.log(`  budget ${name.padEnd(10)} ${old} -> ${next} tokens of the old measurement`);
  }
  if (!Object.keys(budgets).length) {
    console.log('  no budgets declared. Pass --budget name=tokens for each token '
      + 'constant in your code to see it re-baselined');
  }
  for (const line of repairLines(state, r)) console.log(`  repair: ${line}`);
  console.log(`${FINDINGS.has(state) ? 1 : 0} finding(s)`);
  process.exitCode = FINDINGS.has(state) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
