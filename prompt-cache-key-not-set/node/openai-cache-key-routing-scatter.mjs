/**
 * Find OpenAI traffic whose cached share falls as concurrency rises.
 *
 * Read only. One paginated GET against the Usage API, which needs an admin key
 * (sk-admin-...). A project key is rejected by /v1/organization/.
 *
 * Cache lookup is prefix-based and routing-sensitive. Without prompt_cache_key
 * a fleet sprays byte-identical prompts across many backends and each one sees
 * a cold prefix, so the hit rate gets worse as you scale out. The signature is
 * a cached share negatively correlated with the hour's request count. A prefix
 * that is simply unstable gives a flat low share at every load, which is a
 * different note.
 *
 * Hours that follow a gap in traffic are dropped before anything is
 * correlated: they run cold because the entry was evicted while nobody was
 * calling, and leaving them in manufactures the very correlation being tested.
 */
const API = 'https://api.openai.com/v1';

const FINDINGS = new Set(['load-correlated-misses']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Hours since the epoch. Pure. Null if unreadable.
 * The buckets carry start_time as a unix integer, but the same code has to
 * survive an ISO string, and gap detection has to be integer arithmetic:
 * comparing formatted stamps gets 23:00 and 00:00 wrong every night.
 */
export function hourIndex(stamp) {
  if (typeof stamp === 'boolean' || stamp === null || stamp === undefined) return null;
  if (typeof stamp === 'number' && Number.isFinite(stamp)) {
    return Math.floor(Math.trunc(stamp) / 3600);
  }
  const text = String(stamp).trim().replace(' ', 'T');
  if (text.length < 13) return null;
  const head = text.slice(0, 13);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}$/.test(head)) return null;
  const when = Date.parse(`${head}:00:00Z`);
  if (Number.isNaN(when)) return null;
  return Math.floor(when / 3600000);
}

/** Render an hour index back as a UTC stamp. Pure. */
export function hourLabel(index) {
  if (index === null || index === undefined) return 'unknown';
  return `${new Date(Math.trunc(index) * 3600000).toISOString().slice(0, 13)}:00Z`;
}

/** Per project_id and model, one row per active hour, sorted. Pure. */
export function rowsBySeries(buckets) {
  const merged = new Map();
  for (const bucket of buckets ?? []) {
    const index = hourIndex(bucket?.start_time);
    if (index === null) continue;
    for (const result of bucket?.results ?? []) {
      if (!result || typeof result !== 'object') continue;
      const ident = `${result.project_id ?? 'unknown'}\t${result.model ?? 'unknown'}`;
      const cell = `${ident}\t${index}`;
      if (!merged.has(cell)) {
        merged.set(cell, { ident, index, hour: hourLabel(index),
                           requests: 0, input: 0, cached: 0 });
      }
      const row = merged.get(cell);
      row.requests += readInt(result.num_model_requests);
      row.input += readInt(result.input_tokens);
      row.cached += readInt(result.input_cached_tokens);
    }
  }
  const out = new Map();
  for (const row of merged.values()) {
    if (row.requests <= 0 && row.input <= 0) continue;
    if (!out.has(row.ident)) out.set(row.ident, []);
    out.get(row.ident).push(row);
  }
  for (const rows of out.values()) rows.sort((a, b) => a.index - b.index);
  return out;
}

/**
 * Pooled cached share over a set of hours. Pure. Null when nothing ran.
 * Pooled rather than averaged: an hour with nine requests must not carry the
 * same weight as an hour with nine thousand.
 */
export function cachedShare(rows) {
  let input = 0;
  let cached = 0;
  for (const row of rows ?? []) {
    input += readInt(row?.input);
    cached += readInt(row?.cached);
  }
  if (input <= 0) return null;
  return cached / input;
}

/**
 * Hours whose previous hour also carried traffic. Pure.
 * The exclusion that keeps this note off someone else's ground.
 */
export function continuationRows(rows) {
  const active = new Set((rows ?? []).map((r) => readInt(r?.index)));
  return (rows ?? []).filter((r) => active.has(readInt(r?.index) - 1));
}

/** The hours the correlation deliberately threw away. Pure. */
export function resumptionRows(rows) {
  const active = new Set((rows ?? []).map((r) => readInt(r?.index)));
  return (rows ?? []).filter((r) => !active.has(readInt(r?.index) - 1));
}

/** Average ranks, ties shared. Pure. */
function ranks(values) {
  const order = values.map((_v, i) => i).sort((a, b) => values[a] - values[b]);
  const out = new Array(values.length).fill(0);
  let i = 0;
  while (i < order.length) {
    let j = i;
    while (j + 1 < order.length && values[order[j + 1]] === values[order[i]]) j += 1;
    const shared = (i + j) / 2 + 1;
    for (let k = i; k <= j; k += 1) out[order[k]] = shared;
    i = j + 1;
  }
  return out;
}

/**
 * Rank correlation between two equal-length series. Pure. Null if flat.
 *
 * Rank rather than Pearson because request counts are heavy tailed and one
 * incident hour would otherwise decide the answer. The two degenerate cases
 * are deliberately different answers: a load that never varies returns null,
 * because nothing can be said about concurrency from a flat request rate,
 * while a share that never varies returns 0, because "no relationship" is a
 * real finding and it is the one that points at prefix instability.
 */
export function spearman(xs, ys) {
  const a = [...(xs ?? [])];
  const b = [...(ys ?? [])];
  if (a.length !== b.length || a.length < 8) return null;
  const rx = ranks(a);
  const ry = ranks(b);
  const n = a.length;
  const mx = rx.reduce((s, v) => s + v, 0) / n;
  const my = ry.reduce((s, v) => s + v, 0) / n;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < n; i += 1) {
    sxy += (rx[i] - mx) * (ry[i] - my);
    sxx += (rx[i] - mx) ** 2;
    syy += (ry[i] - my) ** 2;
  }
  if (sxx <= 0) return null;
  if (syy <= 0) return 0;
  return sxy / (Math.sqrt(sxx) * Math.sqrt(syy));
}

/**
 * Pooled cached share in the quietest and busiest hours. Pure.
 * Returns [quietShare, busyShare, quietRate, busyRate], or nulls.
 */
export function loadSplit(rows, fraction = 0.33) {
  const active = (rows ?? []).filter((r) => readInt(r?.requests) > 0);
  if (active.length < 6) return [null, null, null, null];
  const ordered = [...active].sort((a, b) => readInt(a?.requests) - readInt(b?.requests));
  const size = Math.max(2, Math.trunc(ordered.length * fraction));
  const quiet = ordered.slice(0, size);
  const busy = ordered.slice(-size);
  const rate = (part) => part.reduce((s, r) => s + readInt(r?.requests), 0) / part.length;
  return [cachedShare(quiet), cachedShare(busy), rate(quiet), rate(busy)];
}

/** Which note owns this shape, when it is not this one. Pure. */
export function handoff(state) {
  if (state === 'no-cached-tokens') {
    return 'not one cached token at any load, so the traffic never becomes '
      + 'eligible rather than being routed away from its cache. Read the '
      + "prompt-below-model-cache-minimum note and check the mean input per "
      + "request against the model's floor first.";
  }
  if (state === 'flat-low-share') {
    return 'the share is low and stays low whatever the load, which is a prefix '
      + 'that differs between calls rather than requests landing on different '
      + 'machines. Read the cache-invalidated-by-changing-prefix note.';
  }
  if (state === 'cold-only-after-idle') {
    return 'the cold hours are the ones that follow gaps in traffic, and the '
      + 'busy hours are fine. That is eviction during idle time: read the '
      + 'prompt-cache-retention-left-at-default note.';
  }
  return '';
}

/** Classify one project and model series. Pure. Returns [state, detail]. */
export function classify(rows, rhoFloor = -0.4, ratioFloor = 0.6,
                         quietFloor = 0.15, minHours = 24) {
  const all = rows ?? [];
  const linked = continuationRows(all);
  if (linked.length < minHours) {
    return ['too-few-linked-hours',
      `${linked.length} hour(s) with traffic in the hour before them, under the `
      + `floor of ${minHours}. Correlating against load needs a run of busy hours.`];
  }

  const overall = cachedShare(linked);
  if (overall !== null && overall <= 0) {
    const input = linked.reduce((s, r) => s + readInt(r?.input), 0);
    return ['no-cached-tokens',
      `${input} input token(s) across ${linked.length} linked hour(s) and not one cached`];
  }

  const [quiet, busy, quietRate, busyRate] = loadSplit(linked);
  const rho = spearman(linked.map((r) => readInt(r?.requests)),
                       linked.map((r) => cachedShare([r]) ?? 0));

  if (rho === null || quiet === null || busy === null) {
    return ['load-does-not-vary',
      'the request rate barely moves across the window, so nothing here can be '
      + 'attributed to concurrency'];
  }

  if (rho <= rhoFloor && quiet >= quietFloor && busy <= quiet * ratioFloor) {
    return ['load-correlated-misses',
      `cached share ${(quiet * 100).toFixed(0)}% in the quietest hours `
      + `(${quietRate.toFixed(0)} req/h) and ${(busy * 100).toFixed(0)}% in the `
      + `busiest (${busyRate.toFixed(0)} req/h), rank correlation `
      + `${rho.toFixed(2)} against request rate. The prefix is cacheable; the `
      + 'requests are not landing where it is cached.'];
  }

  if (rho >= -rhoFloor) {
    return ['share-rises-with-load',
      `cached share climbs with the request rate (${rho.toFixed(2)}): density is `
      + 'keeping entries warm, which is the opposite of scatter'];
  }

  const cold = resumptionRows(all);
  const coldShare = cachedShare(cold);
  if (overall !== null && coldShare !== null && coldShare <= 0.02
      && overall >= quietFloor && cold.length >= 3) {
    return ['cold-only-after-idle',
      `${(overall * 100).toFixed(0)}% cached in linked hours against `
      + `${(coldShare * 100).toFixed(0)}% in the ${cold.length} hour(s) that follow a gap`];
  }

  if (overall !== null && overall < quietFloor) {
    return ['flat-low-share',
      `cached share ${(overall * 100).toFixed(0)}% overall with rank correlation `
      + `${rho.toFixed(2)} against load: low everywhere rather than low under load`];
  }

  return ['healthy',
    `cached share ${(quiet * 100).toFixed(0)}% quiet and ${(busy * 100).toFixed(0)}% `
    + `busy, correlation ${rho.toFixed(2)}`];
}

/** The routing hint, and what makes a good one. Pure. */
export function repairLines() {
  return [
    'set prompt_cache_key on the route: '
    + 'client.responses.create(..., prompt_cache_key="rag-answer-v3").',
    'make it coarse. The template name, or the template plus tenant, so traffic '
    + 'concentrates on a few caches. A per-request id scatters the fleet exactly '
    + 'as badly as no key at all.',
    'keep it out of the prompt. It is a routing hint, not content, and it does '
    + 'not pin a request to a machine or guarantee a hit.',
    'then re-read these same hourly buckets. What should move first is the busy '
    + 'end: the gap between the quiet and busy shares closes before the average does.',
  ];
}

function windowStart(days) {
  const now = new Date();
  now.setUTCMinutes(0, 0, 0);
  return Math.floor((now.getTime() - days * 86400000) / 1000);
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/ needs an admin `
                    + 'key (sk-admin-...), not a project key');
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
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key '
                  + '(sk-admin-...); a project key cannot read /v1/organization/');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(2, Math.min(Number((process.env.DAYS || "dummy-days") ?? 7), 30));
  const rhoFloor = Number((process.env.RHO_FLOOR || "dummy-rho-floor") ?? -0.4);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organization/usage/completions', {
    start_time: windowStart(days),
    bucket_width: '1h',
    limit: 168,
    'group_by[]': ['project_id', 'model'],
  })) buckets.push(bucket);

  const series = rowsBySeries(buckets);
  if (series.size === 0) {
    console.log(`no completions usage in the last ${days} day(s)`);
    return;
  }

  let checked = 0;
  let bad = 0;
  for (const ident of [...series.keys()].sort()) {
    const rows = series.get(ident);
    const [state, detail] = classify(rows, rhoFloor);
    checked += 1;
    const line = `${state.padEnd(24)} ${ident.replace('\t', ' / ')}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      console.warn(`  ${resumptionRows(rows).length} hour(s) that follow a gap in `
                   + 'traffic were excluded before correlating; those run cold '
                   + 'for a different reason.');
      for (const repair of repairLines()) console.warn(`  repair: ${repair}`);
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

  console.log(`${checked} project/model series checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
