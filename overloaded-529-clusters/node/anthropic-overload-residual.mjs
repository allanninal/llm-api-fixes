/**
 * Size the Anthropic requests that were attempted and never served.
 *
 * Read only. One GET against the Admin API, which needs an Admin API key
 * (sk-ant-admin...). The messages usage report carries token sums and no
 * request count, so served requests are estimated from the work that was done:
 * median tokens per attempt, billed tokens divided by it, subtracted from your
 * own attempt counter.
 *
 * Nothing is retried and nothing is sent.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// cache_creation is a nested object; a flat reader sums zero.
const TOKEN_FIELDS = ['uncached_input_tokens', 'input_tokens',
                      'cache_read_input_tokens', 'output_tokens'];
const CACHE_CREATION_FIELDS = ['ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'];

const FINDINGS = new Set(['overload-cluster']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Normalise a timestamp to a UTC minute key. Pure. Null if unreadable.
 * Two sources that disagree about timestamp format produce a comparison with
 * no overlap and a clean bill of health, which is the worst failure here.
 */
export function minuteKey(stamp) {
  if (typeof stamp === 'boolean') return null;
  if (typeof stamp === 'number') {
    if (!Number.isFinite(stamp)) return null;
    const when = new Date(Math.trunc(stamp) * 1000);
    if (Number.isNaN(when.getTime())) return null;
    return `${when.toISOString().slice(0, 16)}Z`;
  }
  const text = String(stamp ?? '').trim().replace(' ', 'T');
  if (text.length < 16) return null;
  const head = text.slice(0, 16);
  if (head[4] !== '-' || head[7] !== '-' || head[10] !== 'T' || head[13] !== ':') return null;
  for (const part of [head.slice(0, 4), head.slice(5, 7), head.slice(8, 10),
                      head.slice(11, 13), head.slice(14, 16)]) {
    if (!/^[0-9]+$/.test(part)) return null;
  }
  return `${head}Z`;
}

/**
 * Minutes since the epoch for a minute key. Pure. Null if unreadable.
 * Adjacency has to be integer arithmetic; string comparison gets 14:59 and
 * 15:00 wrong.
 */
export function minuteIndex(key) {
  const normalised = minuteKey(key);
  if (normalised === null) return null;
  const ms = Date.UTC(Number(normalised.slice(0, 4)), Number(normalised.slice(5, 7)) - 1,
                      Number(normalised.slice(8, 10)), Number(normalised.slice(11, 13)),
                      Number(normalised.slice(14, 16)));
  return Number.isFinite(ms) ? Math.floor(ms / 60000) : null;
}

/** Total billed tokens per minute. Pure. Every field, cache included. */
export function tokensByMinute(buckets) {
  const out = new Map();
  for (const bucket of buckets ?? []) {
    const key = minuteKey(bucket?.starting_at ?? bucket?.start_time);
    if (key === null) continue;
    let total = 0;
    for (const result of bucket?.results ?? []) {
      for (const field of TOKEN_FIELDS) total += readInt(result?.[field]);
      const creation = result?.cache_creation ?? {};
      for (const field of CACHE_CREATION_FIELDS) total += readInt(creation?.[field]);
    }
    out.set(key, (out.get(key) ?? 0) + total);
  }
  return out;
}

/**
 * Read your own attempt counter into minute keys. Pure.
 * Unparseable minutes are dropped rather than folded into a neighbour: a
 * misattributed attempt breaks the contiguity test.
 */
export function attemptsByMinute(raw) {
  const out = new Map();
  for (const [stamp, value] of Object.entries(raw ?? {})) {
    const key = minuteKey(stamp);
    if (key === null) continue;
    let count;
    if (value !== null && typeof value === 'object') count = readInt(value.attempts);
    else if (typeof value === 'boolean') count = 0;
    else count = readInt(value);
    out.set(key, (out.get(key) ?? 0) + count);
  }
  return out;
}

function median(values) {
  const ordered = [...(values ?? [])].sort((a, b) => a - b);
  if (ordered.length === 0) return null;
  const middle = Math.floor(ordered.length / 2);
  if (ordered.length % 2) return ordered[middle];
  return (ordered[middle - 1] + ordered[middle]) / 2;
}

/**
 * Median tokens per attempt across the covered minutes. Pure.
 * The median, never the mean: a mean would be dragged down by the very minutes
 * this is meant to find, so it would come back clean during an outage.
 */
export function baselineTokensPerAttempt(tokens, attempts, minMinutes = 5, minAttempts = 1) {
  const ratios = [];
  for (const [key, value] of attempts ?? new Map()) {
    const made = readInt(value);
    if (made < minAttempts) continue;
    ratios.push(readInt(tokens?.get(key)) / made);
  }
  if (ratios.length < minMinutes) return null;
  const value = median(ratios);
  return value && value > 0 ? value : null;
}

/** One row per minute: attempts, tokens, estimated served and residual. Pure. */
export function residualRows(tokens, attempts, baseline) {
  const out = [];
  if (!baseline || baseline <= 0) return out;
  for (const key of [...(attempts?.keys() ?? [])].sort()) {
    const made = readInt(attempts.get(key));
    if (made <= 0) continue;
    const billed = readInt(tokens?.get(key));
    const served = billed / baseline;
    const residual = Math.max(0, made - served);
    out.push({ minute: key, index: minuteIndex(key), attempts: made, tokens: billed,
               served, residual, share: residual / made });
  }
  return out;
}

/**
 * Group the shortfall minutes into contiguous runs. Pure.
 * Contiguity is the finding: a call spanning a bucket boundary lands its
 * attempt in one minute and its tokens in the next, so isolated minutes are
 * arithmetic rather than overload.
 */
export function clusters(rows, floor = 0.3, minAttempts = 20) {
  const bad = (rows ?? [])
    .filter((r) => r?.index !== null && r?.index !== undefined
      && readInt(r?.attempts) >= minAttempts && Number(r?.share ?? 0) >= floor)
    .sort((a, b) => a.index - b.index);

  const runs = [];
  for (const row of bad) {
    const last = runs[runs.length - 1];
    if (last && row.index === last[last.length - 1].index + 1) last.push(row);
    else runs.push([row]);
  }
  return runs;
}

/** Classify one run of minutes. Pure. Returns [state, detail]. */
export function classify(cluster, minMinutes = 3) {
  const run = cluster ?? [];
  if (run.length === 0) return ['no-cluster', 'nothing to classify'];
  const attempts = run.reduce((sum, r) => sum + readInt(r?.attempts), 0);
  const lost = run.reduce((sum, r) => sum + Number(r?.residual ?? 0), 0);
  const share = attempts ? lost / attempts : 0;
  const detail = `${run[0].minute} through ${run[run.length - 1].minute}: ` +
    `${attempts} attempt(s) over ${run.length} minute(s), about ` +
    `${Math.trunc(lost)} of them produced no billed tokens ` +
    `(${(share * 100).toFixed(0)}%)`;
  if (run.length < minMinutes) {
    return ['single-minute-dip',
      `${detail}. Shorter than the ${minMinutes} minute floor, so this is most ` +
      'likely a request that straddled a bucket boundary rather than a ' +
      'capacity condition.'];
  }
  return ['overload-cluster',
    `${detail}. A run this long is a platform capacity condition, which is ` +
    'what 529 is, and it is retryable.'];
}

/**
 * Minutes where far more work was billed than the attempts explain. Pure.
 * The opposite sign and a different note: a recording gap in your telemetry.
 */
export function excessMinutes(rows, tolerance = 0.25) {
  const out = [];
  for (const row of rows ?? []) {
    const made = readInt(row?.attempts);
    if (made <= 0) continue;
    if (Number(row?.served ?? 0) > made * (1 + tolerance)) out.push(row.minute);
  }
  return out;
}

/** Every service_tier value present in the window. Pure. */
export function tiersSeen(buckets) {
  const out = new Set();
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const tier = String(result?.service_tier ?? '').trim();
      if (tier) out.add(tier);
    }
  }
  return out;
}

function windowStart(minutes) {
  const now = new Date();
  now.setUTCSeconds(0, 0);
  return `${new Date(now.getTime() - minutes * 60000).toISOString().slice(0, 19)}Z`;
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
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
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
    query = { ...query, page: page.next_page };
  }
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const file = (process.env.ATTEMPTS || "dummy-attempts");
  if (!file) {
    console.error('set ATTEMPTS to a JSON file of the requests your client ' +
                  'attempted, keyed by minute');
    process.exitCode = 2;
    return;
  }
  const minutes = Math.max(1, Math.min(Number((process.env.MINUTES || "dummy-minutes") ?? 240), 1440));
  const floor = Number((process.env.FLOOR || "dummy-floor") ?? 0.3);
  const minCluster = Number((process.env.MIN_CLUSTER || "dummy-min-cluster") ?? 3);

  let raw;
  try {
    raw = JSON.parse(await readFile(file, 'utf8'));
  } catch (err) {
    console.error(`could not read ${file}: ${err.message}`);
    process.exitCode = 2;
    return;
  }

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organizations/usage_report/messages', {
    starting_at: windowStart(minutes),
    bucket_width: '1m',
    limit: minutes,
    'group_by[]': ['service_tier'],
  })) buckets.push(bucket);

  const tokens = tokensByMinute(buckets);
  const attempts = attemptsByMinute(raw);
  if (attempts.size === 0) {
    console.error(`no readable minutes in ${file}. Keys should look like ` +
                  '2026-08-30T14:03Z');
    process.exitCode = 2;
    return;
  }

  const baseline = baselineTokensPerAttempt(tokens, attempts);
  if (baseline === null) {
    console.log('not enough overlapping minutes to establish a baseline; nothing ' +
                'can be said about loss in this window');
    return;
  }
  console.log(`baseline ${Math.trunc(baseline)} token(s) per attempt, taken as ` +
              `the median across ${attempts.size} minute(s)`);

  const rows = residualRows(tokens, attempts, baseline);
  let found = 0;
  for (const cluster of clusters(rows, floor)) {
    const [state, detail] = classify(cluster, minCluster);
    if (FINDINGS.has(state)) { found += 1; console.warn(`${state.padEnd(18)} ${detail}`); }
    else console.log(`${state.padEnd(18)} ${detail}`);
  }

  const over = excessMinutes(rows);
  if (over.length > 0) {
    console.warn(`  ${over.length} minute(s) billed far more work than your ` +
                 `attempts explain, starting at ${over[0]}. That is the opposite ` +
                 'sign and a different problem: tokens you were billed for and ' +
                 'did not record.');
  }

  const tiers = tiersSeen(buckets);
  if (tiers.size > 0 && !tiers.has('priority')) {
    console.log(`  no traffic in this window was served as priority ` +
                `(${[...tiers].sort().join(', ')})`);
  }

  if (found) {
    console.warn('  repair: put 429, every 5xx and 529 in one retryable class ' +
                 'with exponential backoff and jitter, or use the SDK own retry ' +
                 'instead of a hand-rolled catch. 529 is overloaded_error and is ' +
                 'a platform capacity condition, not something your request caused.');
    console.warn('  repair: capture the request-id header from every response ' +
                 'including errors. It is the only identifier support can act on, ' +
                 'and this report cannot recover it after the fact.');
  }

  console.log(`${rows.length} minute(s) compared, ${found} cluster(s)`);
  process.exitCode = found ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
