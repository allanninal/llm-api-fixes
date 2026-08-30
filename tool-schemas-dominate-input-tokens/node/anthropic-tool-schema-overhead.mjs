/**
 * Measure what a Claude tools block costs in input tokens on every call.
 *
 * Read only. One GET for the model object and a handful of calls to
 * /v1/messages/count_tokens, which is free, creates no object, generates no
 * completion and is not billed. /v1/messages is never called.
 *
 * Count the body, count it again with tools removed, subtract. Ablate one tool
 * at a time for a per-tool price, and note that the deltas do not sum to the
 * whole: every ablated body still carries the tool-use system prompt.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// Fields the counting endpoint refuses, stripped identically from every body.
const SAMPLING_ONLY = new Set(['max_tokens', 'stream', 'temperature', 'top_p',
  'top_k', 'stop_sequences', 'metadata', 'service_tier']);

// The automatic tool-use system prompt per model, as [autoOrNone, anyOrTool].
// Longest prefix wins: a substring test reads claude-opus-4-5 as claude-opus-5.
const TOOL_SYSTEM_PROMPT = {
  'claude-opus-5': [286, 406],
  'claude-opus-4-8': [290, 410],
  'claude-opus-4-7': [675, 804],
  'claude-opus-4-6': [497, 589],
  'claude-sonnet-4-6': [497, 589],
  'claude-sonnet-5': [354, 474],
  'claude-opus-4-5': [496, 588],
  'claude-sonnet-4-5': [496, 588],
  'claude-haiku-4-5': [496, 588],
};

const FINDINGS = new Set(['schema-dominates', 'schema-heavy']);

/** Read a token count as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** A body the counting endpoint will accept. Pure. Does not mutate. */
export function countable(body) {
  if (!body || typeof body !== 'object') return {};
  const out = {};
  for (const [k, v] of Object.entries(body)) {
    if (!SAMPLING_ONLY.has(k)) out[k] = structuredClone(v);
  }
  return out;
}

/**
 * The same body with the whole tools block removed. Pure.
 * tool_choice goes with it: a body naming a tool it no longer declares is
 * rejected, and the rejection reads as a broken counter.
 */
export function withoutTools(body) {
  const stripped = countable(body);
  delete stripped.tools;
  delete stripped.tool_choice;
  return stripped;
}

/** Named tools in a body, in declaration order. Pure. */
export function toolNames(body) {
  const out = [];
  for (const tool of body?.tools ?? []) {
    if (!tool || typeof tool !== 'object') continue;
    const name = String(tool.name ?? '').trim();
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

/** The same body with exactly one tool removed. Pure. Does not mutate. */
export function withoutTool(body, name) {
  const stripped = countable(body);
  const kept = (stripped.tools ?? []).filter(
    (t) => !(t && typeof t === 'object' && String(t.name ?? '') === String(name)));
  stripped.tools = kept;
  if (kept.length === 0) {
    delete stripped.tools;
    delete stripped.tool_choice;
  }
  return stripped;
}

/** Tokens attributable to the tools block. Pure. Never negative. */
export function overhead(total, base) {
  return Math.max(0, readInt(total) - readInt(base));
}

/** Share of counted input the tools block accounts for. Pure. Null when none. */
export function overheadShare(total, base) {
  const counted = readInt(total);
  if (counted <= 0) return null;
  return overhead(total, base) / counted;
}

/** Which column of the tool-use system prompt table applies. Pure. */
export function choiceKind(body) {
  const choice = body?.tool_choice;
  let kind = '';
  if (typeof choice === 'string') kind = choice.trim().toLowerCase();
  else if (choice && typeof choice === 'object') {
    kind = String(choice.type ?? '').trim().toLowerCase();
  }
  return kind === 'any' || kind === 'tool' ? 'any' : 'auto';
}

/**
 * The automatic tool-use system prompt for one model. Pure. Null if unlisted.
 * Unlisted returns null rather than a neighbour's number: a plausible wrong
 * value here silently corrupts the split between schemas and fixed charge.
 */
export function systemPromptTokens(model, kind = 'auto') {
  const name = String(model ?? '').trim().toLowerCase();
  let best = null;
  let bestLen = -1;
  for (const [prefix, sizes] of Object.entries(TOOL_SYSTEM_PROMPT)) {
    if ((name === prefix || name.startsWith(`${prefix}-`)) && prefix.length > bestLen) {
      best = sizes;
      bestLen = prefix.length;
    }
  }
  if (best === null) return null;
  return String(kind).toLowerCase() === 'any' ? best[1] : best[0];
}

/**
 * The part of the tool overhead that belongs to no single tool. Pure.
 * Returns [residual, measured]. Ablation never removes the tool-use system
 * prompt, so the deltas sum to the schema weight and the rest is fixed.
 */
export function fixedOverhead(totalOverhead, perTool) {
  let measured = 0;
  for (const row of perTool ?? []) measured += Math.max(0, readInt(row?.tokens));
  return [Math.max(0, readInt(totalOverhead) - measured), measured];
}

/** Classify one measured payload. Pure. Returns [state, detail]. */
export function classify(total, base, dominate = 0.5, heavy = 0.25) {
  const counted = readInt(total);
  if (counted <= 0) {
    return ['nothing-counted', 'the counting endpoint returned no tokens for this body'];
  }
  const weight = overhead(total, base);
  if (weight <= 0) {
    return ['no-tools', `${counted} input token(s) and no measurable tools block`];
  }
  const share = weight / counted;
  const rest = counted - weight;
  let shape = `${weight} of ${counted} input token(s) are the tools block ` +
    `(${(share * 100).toFixed(0)}%)`;
  if (rest > 0) {
    shape += `, against ${rest} token(s) of system and messages, a ratio of ` +
      `${(weight / rest).toFixed(1)} to 1`;
  }
  if (share >= dominate) {
    return ['schema-dominates',
      `${shape}. The machinery outweighs the conversation on every call, ` +
      'cached or not.'];
  }
  if (share >= heavy) {
    return ['schema-heavy',
      `${shape}. Not dominant, and still the single largest stable block in ` +
      'the prompt, which makes it the cheapest thing to cache.'];
  }
  return ['schema-modest', `${shape}.`];
}

/**
 * Tools that could carry defer_loading, and never all of them. Pure.
 * The API answers a fully deferred request with 400, "All tools have
 * defer_loading set", so at least one tool always stays eager.
 */
export function deferCandidates(rows, hot = [], keepEager = 1) {
  const names = (rows ?? []).map((r) => String(r?.name ?? '')).filter(Boolean);
  if (names.length <= keepEager) return [];
  const hotSet = new Set((hot ?? []).map(String));
  let candidates = names.filter((n) => !hotSet.has(n));
  if (candidates.length >= names.length) {
    const heaviest = [...rows].sort((a, b) => readInt(b?.tokens) - readInt(a?.tokens));
    const eager = new Set(heaviest.slice(0, Math.max(1, keepEager))
      .map((r) => String(r?.name ?? '')));
    candidates = names.filter((n) => !eager.has(n));
  }
  return candidates;
}

/** What one per-call token count costs in a month. Pure. Null if unpriced. */
export function monthlyCost(tokensPerCall, callsPerDay, ratePerMtok, days = 30) {
  const tokens = readInt(tokensPerCall);
  const calls = readInt(callsPerDay);
  const rate = Number(ratePerMtok);
  if (!Number.isFinite(rate) || tokens <= 0 || calls <= 0 || rate <= 0) return null;
  return (tokens * calls * Math.trunc(days)) / 1000000 * rate;
}

/** Share of the model context window spent before the user speaks. Pure. */
export function windowShare(total, window) {
  const size = readInt(window);
  if (size <= 0) return null;
  return Math.min(1, readInt(total) / size);
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
  if (res.status === 404) return {};
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

/** The one non-GET call. It creates nothing, generates nothing, bills nothing. */
async function count(key, body) {
  const res = await fetch(`${API}/messages/count_tokens`, {
    method: 'POST',  // count_tokens creates nothing and bills nothing
    headers: headers(key),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    console.warn(`count_tokens answered ${res.status}`);
    return null;
  }
  return readInt((await res.json())?.input_tokens);
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
  const callsPerDay = Number((process.env.CALLS_PER_DAY || "dummy-calls-per-day") ?? 10000);
  const inputRate = Number((process.env.INPUT_RATE || "dummy-input-rate") ?? 3.0);
  const hot = String((process.env.HOT || "dummy-hot") ?? '').split(',').filter(Boolean);
  const perTool = (process.env.NO_PER_TOOL || "dummy-no-per-tool") !== '1';

  let checked = 0;
  let bad = 0;
  for (const path of paths) {
    const body = JSON.parse(await readFile(path, 'utf8'));
    checked += 1;

    const total = await count(key, countable(body));
    const base = await count(key, withoutTools(body));
    if (total === null || base === null) {
      console.warn(`could not measure ${path}`);
      continue;
    }

    const [state, detail] = classify(total, base);
    const line = `${state.padEnd(18)} ${path.padEnd(24)} ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
    } else {
      console.log(line);
    }

    const model = String(body.model ?? '');
    const kind = choiceKind(body);
    const fixed = systemPromptTokens(model, kind);
    if (fixed === null) {
      console.log(`  no published tool-use system prompt size for ${model}, so ` +
                  'the fixed charge cannot be separated out here');
    } else {
      console.log(`  ${fixed} of the overhead is the automatic tool-use system ` +
                  `prompt for ${model} at tool_choice ${kind}`);
    }

    let rows = [];
    if (perTool) {
      for (const name of toolNames(body)) {
        const one = await count(key, withoutTool(body, name));
        if (one === null) continue;
        rows.push({ name, tokens: Math.max(0, total - one) });
      }
      rows.sort((a, b) => b.tokens - a.tokens);
      const [residual, measured] = fixedOverhead(overhead(total, base), rows);
      console.log(`  the fixed charge no ablation removes: ${residual} token(s); ` +
                  `your schemas account for ${measured}`);
      if (rows.length > 0) {
        console.log(`  heaviest: ${rows.slice(0, 3)
          .map((r) => `${r.name} ${r.tokens}`).join(', ')}`);
      }
    }

    const window = model ? (await get(key, `/models/${model}`))?.max_input_tokens : null;
    const share = windowShare(total, window);
    if (share !== null) {
      console.log(`  ${(share * 100).toFixed(0)}% of the ${readInt(window)} token ` +
                  'context window is spent before the user says anything. Whether ' +
                  'a real conversation still fits is the context-overflow ' +
                  'question, not this one.');
    }

    const price = monthlyCost(overhead(total, base), callsPerDay, inputRate);
    if (price !== null) {
      console.log(`  at ${callsPerDay} call(s) a day and ${inputRate.toFixed(2)} ` +
                  `per million input tokens that is ${price.toFixed(2)} a month, uncached`);
    }

    if (FINDINGS.has(state)) {
      console.warn('  repair: put a cache_control breakpoint after the tools ' +
                   'block. A read costs 0.1x base input, and tools are the most ' +
                   'stable thing in the prompt.');
      console.warn('  repair: editing any tool description after that ' +
                   'invalidates the tools, the system prompt and the messages ' +
                   'behind them. Batch tool edits.');
      const candidates = deferCandidates(rows, hot);
      if (candidates.length > 0) {
        console.warn(`  repair: defer_loading on rarely used tools only ` +
                     `(${candidates.slice(0, 5).join(', ')}). Never on all of ` +
                     'them: the API answers 400, All tools have defer_loading ' +
                     'set. Which are rare is a call-coverage question this ' +
                     'script cannot answer.');
      }
    }
  }

  console.log(`${checked} payload(s) measured, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
