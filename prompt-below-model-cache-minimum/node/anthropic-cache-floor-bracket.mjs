/**
 * Bracket a cached prefix against the cache minimums of the models it runs on.
 *
 * Read only. One GET against the Admin API, which needs an Admin API key
 * (sk-ant-admin...). A workspace key is rejected by /v1/organizations/.
 *
 * A prefix under a model's minimum cacheable length is silently not cached.
 * The messages usage report has no request count, so the prefix cannot be
 * measured from it. It can be bracketed: one key that runs several models
 * sends the same prefix to all of them, so caching that works below one floor
 * and stops above another puts the prefix between the two.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const FINDINGS = new Set(['below-cache-minimum']);

/**
 * Published minimum cacheable prompt length per model family, in tokens.
 * A model absent from this table gets no floor and is left out of the verdict
 * rather than guessed at.
 */
export const CACHE_MINIMUMS = {
  'claude-opus-5': 512,
  'claude-fable-5': 512,
  'claude-mythos-5': 512,
  'claude-mythos-preview': 2048,
  'claude-opus-4-8': 1024,
  'claude-opus-4-7': 2048,
  'claude-opus-4-6': 4096,
  'claude-opus-4-5': 4096,
  'claude-opus-4-1': 1024,
  'claude-opus-4': 1024,
  'claude-sonnet-5': 1024,
  'claude-sonnet-4-6': 1024,
  'claude-sonnet-4-5': 1024,
  'claude-sonnet-4': 1024,
  'claude-haiku-4-5': 4096,
  'claude-haiku-3-5': 2048,
};

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * The model's minimum cacheable prompt length. Pure. Null if unrecognised.
 * Longest prefix match, because usage reports carry dated snapshot ids, and
 * null has to mean "no opinion" rather than "floor zero" downstream.
 */
export function cacheMinimum(model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return null;
  let best = null;
  for (const [family, floor] of Object.entries(CACHE_MINIMUMS)) {
    if (name === family || name.startsWith(`${family}-`)) {
      if (best === null || family.length > best[0].length) best = [family, floor];
    }
  }
  return best ? best[1] : null;
}

/** Per api_key_id and model, the window's token totals. Pure. */
export function series(buckets) {
  const out = new Map();
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      if (!result || typeof result !== 'object') continue;
      const ident = `${result.api_key_id ?? 'unknown'}\t${result.model ?? 'unknown'}`;
      if (!out.has(ident)) out.set(ident, { uncached: 0, writes: 0, reads: 0 });
      const row = out.get(ident);
      const creation = result.cache_creation ?? {};
      row.uncached += readInt(result.uncached_input_tokens);
      row.writes += readInt(creation.ephemeral_5m_input_tokens)
        + readInt(creation.ephemeral_1h_input_tokens);
      row.reads += readInt(result.cache_read_input_tokens);
    }
  }
  return out;
}

/** Regroup the series into one list of model rows per api_key_id. Pure. */
export function byKey(totals) {
  const out = new Map();
  for (const [ident, row] of totals ?? new Map()) {
    const [key, model] = String(ident).split('\t');
    if (!out.has(key)) out.set(key, []);
    out.get(key).push({
      model,
      floor: cacheMinimum(model),
      uncached: readInt(row?.uncached),
      writes: readInt(row?.writes),
      reads: readInt(row?.reads),
    });
  }
  for (const rows of out.values()) {
    rows.sort((a, b) => (a.floor ?? 1e9) - (b.floor ?? 1e9)
      || a.model.localeCompare(b.model));
  }
  return out;
}

/** Models that cache for at least one key in the org. Pure. The cross-key control. */
export function modelsCachingAnywhere(totals) {
  const out = new Set();
  for (const [ident, row] of totals ?? new Map()) {
    if (readInt(row?.writes) + readInt(row?.reads) > 0) {
      out.add(String(ident).split('\t')[1]);
    }
  }
  return out;
}

/** Sort a key's models into caching, silent and unusable. Pure. */
export function splitRows(rows, minInput = 100000) {
  const caching = [];
  const silent = [];
  const skipped = [];
  for (const row of rows ?? []) {
    if (row?.floor === null || row?.floor === undefined) skipped.push(row);
    else if (readInt(row?.writes) + readInt(row?.reads) > 0) caching.push(row);
    else if (readInt(row?.uncached) >= minInput) silent.push(row);
    else skipped.push(row);
  }
  return { caching, silent, skipped };
}

/**
 * Bracket the cached prefix between two floors. Pure. Null if it does not.
 * The bracket exists only when the split is clean: every silent model above
 * every caching one. A silent model beneath a caching one means the prompt
 * already cleared the higher bar, so size is not the story.
 */
export function floorBracket(caching, silent) {
  if (!caching?.length || !silent?.length) return null;
  const lo = Math.max(...caching.map((r) => readInt(r?.floor)));
  const hi = Math.min(...silent.map((r) => readInt(r?.floor)));
  if (hi <= lo) return null;
  return [lo, hi];
}

/** Which note owns this shape, when it is not this one. Pure. */
export function handoff(state) {
  if (state === 'no-caching-anywhere') {
    return 'this key writes and reads nothing on any model, so there is no '
      + 'contrast to bracket against. Read the prompt-caching-never-used note: '
      + 'with no cache_control anywhere the floors are irrelevant.';
  }
  if (state === 'silent-model-under-a-caching-floor') {
    return 'a model with a lower floor is silent while a model with a higher '
      + 'floor caches, so the prefix cleared the higher bar and size cannot be '
      + 'the reason. Read the cache-invalidated-by-changing-prefix note.';
  }
  if (state === 'peer-caches-same-model') {
    return 'another key in this organization caches on this same model, so the '
      + "model's floor is not the obstacle. Read the "
      + 'cache-invalidated-by-changing-prefix note.';
  }
  if (state === 'single-silent-model') {
    return 'one model and no contrast, so this check cannot separate a prefix '
      + 'under the floor from caching that was never switched on. Both '
      + 'prompt-caching-never-used and this note remain open. Route a sample of '
      + 'the traffic through a model with a lower floor and the ambiguity '
      + 'resolves itself.';
  }
  return '';
}

/** Classify one api_key_id. Pure. Returns [state, detail]. */
export function classify(rows, cachingModels = new Set(), minInput = 100000) {
  const { caching, silent, skipped } = splitRows(rows, minInput);
  if (!caching.length && !silent.length) {
    return ['too-little-traffic',
      `${skipped.length} model(s) seen, none with a known floor and enough input to judge`];
  }

  if (!caching.length) {
    if (silent.length === 1) {
      const row = silent[0];
      if (cachingModels?.has?.(row.model)) {
        return ['peer-caches-same-model',
          `silent on ${row.model} (floor ${readInt(row.floor)}) while another `
          + 'key caches on the same model'];
      }
      return ['single-silent-model',
        `silent on ${row.model} (floor ${readInt(row.floor)}) and running `
        + 'nothing else, so there is no second floor to bracket against'];
    }
    return ['no-caching-anywhere',
      `silent on all ${silent.length} model(s) with known floors: `
      + silent.map((r) => r.model).join(', ')];
  }

  if (!silent.length) {
    return ['caches-on-every-model',
      `cache activity on all ${caching.length} model(s) with known floors`];
  }

  const bracket = floorBracket(caching, silent);
  if (bracket === null) {
    const low = silent.reduce((a, b) => (readInt(a.floor) <= readInt(b.floor) ? a : b));
    const high = caching.reduce((a, b) => (readInt(a.floor) >= readInt(b.floor) ? a : b));
    return ['silent-model-under-a-caching-floor',
      `${low.model} (floor ${readInt(low.floor)}) is silent while ${high.model} `
      + `(floor ${readInt(high.floor)}) caches`];
  }

  const [lo, hi] = bracket;
  const loNames = caching.filter((r) => readInt(r.floor) === lo).map((r) => r.model).join(', ');
  const hiNames = silent.filter((r) => readInt(r.floor) === hi).map((r) => r.model).join(', ');
  return ['below-cache-minimum',
    `caching works up to a floor of ${lo} (${loNames}) and stops at ${hi} `
    + `(${hiNames}), so the cached prefix is at least ${lo} tokens and under `
    + `${hi}. cache_control is being accepted and ignored above the boundary.`];
}

/** The two honest repairs, sized to the bracket. Pure. */
export function repairLines(bracket) {
  if (!bracket) return [];
  const [lo, hi] = bracket;
  return [
    'move more genuinely stable material in front of the last cache_control '
    + `breakpoint until the prefix clears ${hi} tokens: full tool schemas, `
    + 'few-shot examples, retrieval instructions.',
    'or drop cache_control on the routes above the boundary so the code is '
    + 'honest about not caching there, and stop budgeting for a discount that '
    + 'cannot arrive.',
    `do not pad with filler to cross ${hi}. Padding is billed at the full input `
    + 'rate on the write and only pays back at high repeat volume.',
    `the bracket is ${lo} to ${hi} tokens. If that straddles a route you thought `
    + 'was much longer, the prefix is being truncated or rebuilt somewhere '
    + 'before the breakpoint.',
  ];
}

function windowStart(days) {
  const now = new Date();
  now.setUTCHours(0, 0, 0, 0);
  return `${new Date(now.getTime() - days * 86400000).toISOString().slice(0, 19)}Z`;
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/ needs an `
                    + 'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* readBuckets(key, path, params) {
  let query = { ...params };
  for (;;) {
    const page = await get(key, path, query);
    for (const bucket of page?.data ?? []) yield bucket;
    if (!page?.has_more || !page?.next_page) return;
    query = { ...params, page: page.next_page };
  }
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); '
                  + 'a workspace key cannot read /v1/organizations/');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(2, Math.min(Number((process.env.DAYS || "dummy-days") ?? 30), 90));
  const minInput = Number((process.env.MIN_INPUT || "dummy-min-input") ?? 100000);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organizations/usage_report/messages', {
    starting_at: windowStart(days),
    bucket_width: '1d',
    limit: days + 1,
    'group_by[]': ['model', 'api_key_id'],
  })) buckets.push(bucket);

  const totals = series(buckets);
  if (totals.size === 0) {
    console.log(`no messages usage in the last ${days} day(s)`);
    return;
  }

  const cachingModels = modelsCachingAnywhere(totals);
  const keyed = byKey(totals);

  let checked = 0;
  let bad = 0;
  for (const key of [...keyed.keys()].sort()) {
    const rows = keyed.get(key);
    const [state, detail] = classify(rows, cachingModels, minInput);
    checked += 1;
    const line = `${state.padEnd(32)} ${key}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      const { caching, silent } = splitRows(rows, minInput);
      for (const repair of repairLines(floorBracket(caching, silent))) {
        console.warn(`  repair: ${repair}`);
      }
      console.warn('  note: the bracket assumes one prefix per key. A key that '
                   + 'sends a different prompt per model brackets nothing, and '
                   + 'the report cannot see inside a key.');
    } else {
      const note = handoff(state);
      if (note) {
        console.log(line);
        console.log(`  ${note}`);
      } else if (showAll) {
        console.log(line);
      }
    }
  }

  console.log(`${checked} key(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
