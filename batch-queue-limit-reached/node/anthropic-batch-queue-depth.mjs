/**
 * Measure live batch queue depth against the organization's enqueued ceiling.
 *
 * Read only. GET /v1/organizations/rate_limits?group_type=batch with an Admin
 * key for the ceiling, GET /v1/messages/batches with a workspace key for the
 * depth. Nothing is submitted and nothing is cancelled.
 *
 * A batch request is part of the processing queue when it has yet to be
 * successfully processed by the model, which is request_counts.processing.
 *
 * The ceiling is organization wide and the batch list is workspace scoped, so
 * a single workspace key produces a lower bound. Extra keys tighten it.
 */
const RATE_LIMITS_URL = 'https://api.anthropic.com/v1/organizations/rate_limits';
const BATCHES_URL = 'https://api.anthropic.com/v1/messages/batches';

export const LIVE_STATES = new Set(['in_progress', 'canceling']);

export const PER_BATCH_REQUESTS = 100000;
export const PER_BATCH_MB = 256;

const FINDINGS = new Set(['queue-exhausted', 'queue-near-limit', 'queue-limit-unknown']);

/** The enqueued_batch_requests value, or null. Pure. */
export function enqueuedLimit(payload) {
  for (const group of (payload ?? {}).data ?? []) {
    if (!group || typeof group !== 'object') continue;
    if (group.group_type !== 'batch') continue;
    for (const limit of group.limits ?? []) {
      if (limit && typeof limit === 'object' && limit.type === 'enqueued_batch_requests') {
        const value = Number(limit.value);
        return Number.isFinite(value) ? Math.trunc(value) : null;
      }
    }
  }
  return null;
}

/** Live batches and what each holds in the queue. Pure. */
export function queueRows(batches, workspace = '') {
  return (batches ?? [])
    .filter((b) => LIVE_STATES.has(String((b ?? {}).processing_status ?? '')))
    .map((b) => ({
      id: String(b.id),
      status: String(b.processing_status),
      processing: Number((b.request_counts ?? {}).processing) || 0,
      workspace,
    }))
    .sort((a, b) => (b.processing - a.processing) || a.id.localeCompare(b.id));
}

/** Total requests waiting on the model. Pure. */
export function queueDepth(rows) {
  return (rows ?? []).reduce((n, r) => n + (Number(r?.processing) || 0), 0);
}

/** [remaining, occupancy] or [null, null] when the ceiling is unknown. Pure. */
export function headroom(depth, limit) {
  if (limit === null || limit === undefined || limit <= 0) return [null, null];
  return [Math.max(0, limit - depth), depth / limit];
}

/** The n biggest contributors. Pure. */
export function topHolders(rows, n = 3) {
  return (rows ?? []).slice(0, Math.max(0, n)).filter((r) => (Number(r.processing) || 0) > 0);
}

/** Deduplicated workspace credentials. Pure. Order kept. */
export function workspaceKeys(primary, extra) {
  const out = [];
  const seen = new Set();
  for (const candidate of [primary, ...String(extra ?? '').split(',')]) {
    const key = (candidate ?? '').trim();
    if (key && !seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(depth, limit, rows, workspaces, threshold) {
  const [remaining, occupancy] = headroom(depth, limit);
  if (limit === null || limit === undefined) {
    return ['queue-limit-unknown',
      `${depth} batch requests are in the processing queue across ${workspaces} `
      + 'workspace(s), but the enqueued_batch_requests ceiling could not be read, '
      + 'so there is no headroom to report'];
  }
  const percent = Math.round(occupancy * 100);
  if (depth >= limit) {
    return ['queue-exhausted',
      `${depth} of ${limit} enqueued batch requests are in the processing queue, `
      + 'which is the whole ceiling. New submissions are being refused'];
  }
  if (percent >= threshold) {
    return ['queue-near-limit',
      `${depth} of ${limit} enqueued batch requests are in the processing queue, `
      + `which is ${percent}% of the ceiling`];
  }
  return ['queue-clear',
    `${depth} of ${limit} enqueued batch requests are in the processing queue, `
    + `leaving ${remaining} requests of headroom across ${(rows ?? []).length} `
    + 'live batch(es)'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, rows, limit) {
  if (state === 'queue-clear') {
    return ['nothing to change. Keep the check running through the batch window '
      + 'rather than once a day: this is a queue that drains.'];
  }
  if (state === 'queue-limit-unknown') {
    return ['read the ceiling with an Admin key: GET '
      + '/v1/organizations/rate_limits?group_type=batch returns '
      + 'enqueued_batch_requests for the organization. Workspace keys are '
      + 'rejected by every Admin endpoint.',
    'without the ceiling this run is a raw count. It cannot tell you whether '
      + 'the next submission will be accepted.'];
  }
  const lines = ['hold at most a few batches in flight and wait for one to end '
    + 'before submitting the next. A batch request leaves the queue only when '
    + 'the model has processed it.'];
  const biggest = topHolders(rows, 1);
  if (biggest.length && limit) {
    lines.push(`${biggest[0].id} alone holds ${biggest[0].processing} of the `
      + `${limit}. Split submissions of that size: the per batch cap is `
      + `${PER_BATCH_REQUESTS} requests or ${PER_BATCH_MB} MB, whichever comes first.`);
  }
  lines.push('a queue held at the ceiling also slows what is already in it, and '
    + 'slowed batches are the ones that run out of their 24 hour window. '
    + 'Draining is the fix for both.');
  return lines;
}

async function getJson(url, headers, params) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) target.searchParams.set(k, String(v));
  let res;
  try {
    res = await fetch(target, { headers });
  } catch (err) {
    return [null, `request failed: ${err.message}`];
  }
  if (res.status !== 200) return [null, `HTTP ${res.status}`];
  try {
    return [await res.json(), null];
  } catch {
    return [null, 'response was not JSON'];
  }
}

async function readCeiling(adminKey, maxPages = 5) {
  const headers = { 'x-api-key': adminKey, 'anthropic-version': '2023-06-01' };
  let params = { group_type: 'batch' };
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const [payload, err] = await getJson(RATE_LIMITS_URL, headers, params);
    if (err) return [null, err];
    const found = enqueuedLimit(payload);
    if (found !== null) return [found, null];
    if (!payload.next_page) return [null, 'no batch group in the rate limits response'];
    params = { group_type: 'batch', page: payload.next_page };
  }
  return [null, 'the rate limits response never carried a batch group'];
}

async function readBatches(key, maxPages = 20) {
  const headers = { 'x-api-key': key, 'anthropic-version': '2023-06-01' };
  const rows = [];
  let after = null;
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const params = { limit: 1000 };
    if (after) params.after_id = after;
    const [payload, err] = await getJson(BATCHES_URL, headers, params);
    if (err) return [rows, err];
    const data = payload.data ?? [];
    rows.push(...data);
    if (!payload.has_more || !data.length) break;
    after = payload.last_id ?? data[data.length - 1]?.id;
    if (!after) break;
  }
  return [rows, null];
}

function args(argv) {
  const out = { threshold: 80, maxPages: 20 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--threshold') out.threshold = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--max-pages') out.maxPages = Number.parseInt(argv[i += 1], 10);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const adminKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  const keys = workspaceKeys((process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key"),
    (process.env.ANTHROPIC_EXTRA_WORKSPACE_KEYS || "dummy-anthropic-extra-workspace-keys"));
  if (!keys.length) {
    console.error('set ANTHROPIC_API_KEY to a workspace key. Add '
      + 'ANTHROPIC_EXTRA_WORKSPACE_KEYS as a comma separated list to cover more '
      + 'of the organization');
    process.exitCode = 2;
    return;
  }

  let limit = null;
  if (adminKey) {
    const [found, err] = await readCeiling(adminKey);
    if (err) console.log(`could not read the ceiling: ${err}`);
    limit = found;
  } else {
    console.log('no ANTHROPIC_ADMIN_KEY, so the enqueued_batch_requests ceiling '
      + 'cannot be read and only the raw depth is available');
  }
  if (limit !== null) {
    console.log(`ceiling      enqueued_batch_requests ${limit} (organization wide)`);
  }

  const rows = [];
  for (const [index, key] of keys.entries()) {
    const [batches, err] = await readBatches(key, opts.maxPages);
    if (err) console.log(`workspace ${index + 1} batch list stopped early: ${err}`);
    rows.push(...queueRows(batches, `ws${index + 1}`));
  }
  rows.sort((a, b) => (b.processing - a.processing) || a.id.localeCompare(b.id));

  for (const row of rows) {
    console.log(`${row.id.slice(0, 16).padEnd(16)} ${row.status.padEnd(13)} `
      + `${row.processing} processing`);
  }

  const depth = queueDepth(rows);
  const [state, detail] = verdict(depth, limit, rows, keys.length, opts.threshold);
  console.log(`${state.padEnd(20)} ${detail}`);
  console.log('  measured: enqueued_batch_requests from the Rate Limits API, and '
    + `the sum of request_counts.processing over ${rows.length} live batch(es) `
    + `in ${keys.length} workspace(s)`);
  console.log('  inferred: nothing about workspaces whose keys were not '
    + 'supplied. The ceiling is organization wide and this depth is a lower '
    + 'bound on it');
  for (const line of repairLines(state, rows, limit)) console.log(`  repair: ${line}`);

  console.log(`${FINDINGS.has(state) ? 1 : 0} finding(s)`);
  process.exitCode = FINDINGS.has(state) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
