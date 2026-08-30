/**
 * Sum every page of the file store and grade it against a documented ceiling.
 *
 * Read only. GET /v1/files and nothing else, on either provider. Nothing is
 * uploaded, nothing is deleted, and no file content is ever fetched.
 *
 * Neither provider exposes the quota, so the ceiling is a documented constant
 * that no request can confirm while the total is measured by summing a field
 * over every page. The output keeps those two in different columns.
 */
const ENDPOINTS = {
  openai: 'https://api.openai.com/v1/files',
  anthropic: 'https://api.anthropic.com/v1/files',
};

export const DOC_QUOTA_BYTES = { openai: 2_500_000_000_000, anthropic: 1_000_000_000_000 };
export const DOC_QUOTA_LABEL = { openai: '2.5 TB per project',
                                 anthropic: '1 TB per organization' };
export const DOC_FILE_CAP_BYTES = { openai: 512_000_000, anthropic: 500_000_000 };
const SIZE_FIELD = { openai: 'bytes', anthropic: 'size_bytes' };
const KEY_ENV = { openai: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY' };

const FINDINGS = new Set(['quota-critical', 'quota-warning', 'purpose-dominates',
  'file-near-cap', 'no-expiry-policy']);

/** Seconds since the epoch from either provider's shape. Pure. */
export function epoch(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
    return 0;
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;
  }
  const ms = Date.parse(String(value).trim());
  return Number.isFinite(ms) ? Math.max(0, Math.trunc(ms / 1000)) : 0;
}

/** One file object, normalised. Pure. Two providers, one shape. */
export function fileRow(body, provider) {
  const row = (body && typeof body === 'object') ? body : {};
  const size = Number(row[SIZE_FIELD[provider] ?? 'bytes']);
  const expires = epoch(row.expires_at);
  return {
    id: String(row.id ?? ''),
    filename: String(row.filename ?? ''),
    size: Number.isFinite(size) ? Math.max(0, Math.trunc(size)) : 0,
    purpose: String(row.purpose ?? 'unclassified'),
    created_at: epoch(row.created_at),
    expires_at: expires || null,
    expiry_reported: Object.prototype.hasOwnProperty.call(row, 'expires_at'),
  };
}

/** Binary units, one decimal. Pure. */
export function human(size) {
  let n = Number(size);
  if (!Number.isFinite(n)) return '0 B';
  for (const unit of ['B', 'KiB', 'MiB', 'GiB', 'TiB']) {
    if (Math.abs(n) < 1024 || unit === 'TiB') {
      return unit === 'B' ? `${Math.trunc(n)} B` : `${n.toFixed(1)} ${unit}`;
    }
    n /= 1024;
  }
  return `${n.toFixed(1)} TiB`;
}

/** Count and summed bytes. Pure. */
export function totals(rows) {
  const list = rows ?? [];
  return { count: list.length,
           bytes: list.reduce((a, r) => a + Number(r?.size ?? 0), 0) };
}

/** Per-purpose count and bytes, largest first. Pure. */
export function byPurpose(rows) {
  const acc = new Map();
  for (const row of rows ?? []) {
    const key = String(row?.purpose ?? 'unclassified');
    const cur = acc.get(key) ?? { count: 0, bytes: 0 };
    cur.count += 1;
    cur.bytes += Number(row?.size ?? 0);
    acc.set(key, cur);
  }
  return [...acc.entries()]
    .map(([k, v]) => [k, v.count, v.bytes])
    .sort((a, b) => (b[2] - a[2]) || String(a[0]).localeCompare(String(b[0])));
}

/** Share of a documented ceiling. Pure. The ceiling is an argument. */
export function gradeTotal(totalBytes, quotaBytes, warnShare = 0.60, criticalShare = 0.85) {
  const quota = Number(quotaBytes);
  const used = Number(totalBytes);
  if (!Number.isFinite(quota) || !Number.isFinite(used) || quota <= 0) {
    return ['quota-unknown',
      'no usable ceiling was supplied, so the total is a number without a denominator'];
  }
  const share = used / quota;
  const headroom = human(Math.max(0, quota - used));
  const detail = `${(share * 100).toFixed(1)}% of the documented ceiling is in use, `
    + `with about ${headroom} of headroom before uploads start to fail`;
  if (share >= criticalShare) return ['quota-critical', detail];
  if (share >= warnShare) return ['quota-warning', detail];
  return ['quota-headroom',
    `${(share * 100).toFixed(1)}% of the documented ceiling is in use, ${headroom} of headroom`];
}

/** The purpose class worth sweeping first. Pure. */
export function gradeConcentration(purposes, totalBytes, share = 0.40) {
  const total = Number(totalBytes) || 0;
  if (!(purposes ?? []).length || total <= 0) {
    return ['purpose-even',
      'nothing to concentrate: the store is empty or carries no size information'];
  }
  const [name, count, size] = purposes[0];
  const got = size / total;
  if (got < share) {
    return ['purpose-even',
      `no single purpose holds more than ${(share * 100).toFixed(0)}% of the store; `
      + `the largest is ${name} at ${(got * 100).toFixed(1)}%`];
  }
  return ['purpose-dominates',
    `${name} is ${(got * 100).toFixed(1)}% of the store, ${count} file(s)`];
}

/** The second ceiling, per file and not a fraction of the first. Pure. */
export function gradeOutliers(rows, capBytes, warnShare = 0.80) {
  const cap = Number(capBytes);
  if (!Number.isFinite(cap) || cap <= 0) {
    return ['cap-unknown', 'no per-file cap was supplied', []];
  }
  const floor = cap * warnShare;
  const big = (rows ?? []).filter((r) => Number(r?.size ?? 0) >= floor)
    .sort((a, b) => Number(b.size) - Number(a.size));
  if (!big.length) {
    return ['file-sizes-fine',
      `no file is within ${(warnShare * 100).toFixed(0)}% of the per-file cap`, []];
  }
  return ['file-near-cap',
    `${big.length} file(s) above ${(warnShare * 100).toFixed(0)}% of the per-file cap`,
    big];
}

/** The only reading here that describes the future. Pure. */
export function gradeExpiry(rows, now, staleDays) {
  const list = rows ?? [];
  if (!list.length) return ['expiry-none', 'the store is empty'];
  const unexpiring = list.filter((r) => !r?.expires_at);
  if (!unexpiring.length) {
    return ['expiry-covered',
      'every file carries an expires_at, so this store has a lifecycle rather '
      + 'than a trajectory'];
  }
  const cutoff = Number(now) - Number(staleDays) * 86400;
  const stale = unexpiring.filter((r) => Number(r?.created_at ?? 0) > 0
    && Number(r.created_at) < cutoff);
  return ['no-expiry-policy',
    `${unexpiring.length} of ${list.length} file(s) have no expires_at, and `
    + `${stale.length} of those are older than ${Number(staleDays)} day(s)`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'quota-critical' || state === 'quota-warning') {
    return ['sweep the purpose class named below, then set an expiry at upload so '
      + 'the next two thirds take longer to arrive than the last did.'];
  }
  if (state === 'purpose-dominates') {
    return ['delete the ones whose job is finished and read, one at a time, with '
      + 'DELETE /v1/files/{file_id}. Nothing here does that for you, a deleted '
      + 'file cannot be recovered, and on OpenAI the deletion also removes the '
      + 'file from every vector store holding it.'];
  }
  if (state === 'file-near-cap') {
    return ['a second ceiling, unrelated to the total. Split these at source '
      + 'rather than making room for them.'];
  }
  if (state === 'no-expiry-policy') {
    return ['upload with an expiry so this population stops being unbounded: '
      + 'expires_after with an anchor of created_at on OpenAI (3600 to 2592000 '
      + 'seconds), expires_in_seconds on Anthropic (3600 to 7776000).',
    'for what is already there, confirm by hand and then delete. Nothing in the '
      + 'metadata can tell an audit which files matter.'];
  }
  if (state === 'quota-unknown') {
    return ['pass --quota-bytes. Without a denominator this run is an inventory '
      + 'rather than an audit.'];
  }
  return [];
}

async function fetchOpenai(key, maxPages) {
  const rows = [];
  let cursor = null;
  let pages = 0;
  while (pages < maxPages) {
    const url = new URL(ENDPOINTS.openai);
    url.searchParams.set('limit', '10000');
    url.searchParams.set('order', 'asc');
    if (cursor) url.searchParams.set('after', cursor);
    let res;
    try {
      res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
    } catch (err) {
      console.error(`openai listing failed: ${err.message}`);
      return [rows, pages, false];
    }
    if (res.status !== 200) {
      console.error(`openai listing returned HTTP ${res.status}`);
      return [rows, pages, false];
    }
    const body = await res.json().catch(() => ({}));
    const data = body.data ?? [];
    pages += 1;
    for (const item of data) rows.push(fileRow(item, 'openai'));
    if (body.has_more === false || !data.length) return [rows, pages, true];
    if (!('has_more' in body) && data.length < 10000) return [rows, pages, true];
    cursor = data[data.length - 1]?.id;
    if (!cursor) return [rows, pages, true];
  }
  return [rows, pages, false];
}

async function fetchAnthropic(key, maxPages) {
  const rows = [];
  let page = null;
  let pages = 0;
  const headers = { 'x-api-key': key, 'anthropic-version': '2023-06-01' };
  while (pages < maxPages) {
    const url = new URL(ENDPOINTS.anthropic);
    url.searchParams.set('limit', '1000');
    if (page) url.searchParams.set('page', page);
    let res;
    try {
      res = await fetch(url, { headers });
    } catch (err) {
      console.error(`anthropic listing failed: ${err.message}`);
      return [rows, pages, false];
    }
    if (res.status !== 200) {
      console.error(`anthropic listing returned HTTP ${res.status}`);
      return [rows, pages, false];
    }
    const body = await res.json().catch(() => ({}));
    const data = body.data ?? [];
    pages += 1;
    for (const item of data) rows.push(fileRow(item, 'anthropic'));
    page = body.next_page;
    if (!page) return [rows, pages, true];
  }
  return [rows, pages, false];
}

function report(provider, rows, pages, complete, opts, now) {
  const quota = opts.quotaBytes || DOC_QUOTA_BYTES[provider];
  const cap = opts.fileCapBytes || DOC_FILE_CAP_BYTES[provider];
  const tot = totals(rows);
  console.log(`${provider.padEnd(9)} ${pages} page(s) read, ${tot.count} file(s), `
    + `${human(tot.bytes)}`);
  console.log(`  measured: the sum of ${SIZE_FIELD[provider]} over every page of `
    + 'GET /v1/files');
  console.log(`  documented: a ceiling of ${DOC_QUOTA_LABEL[provider]}, which no `
    + 'endpoint reports');
  if (!complete) {
    console.log(`  incomplete: paging stopped early, so ${human(tot.bytes)} is a `
      + 'floor and not a total');
  }

  const [outlierState, outlierDetail, big] = gradeOutliers(rows, cap);
  const grades = [gradeTotal(tot.bytes, quota),
                  gradeConcentration(byPurpose(rows), tot.bytes),
                  [outlierState, outlierDetail],
                  gradeExpiry(rows, now, opts.staleDays)];
  let findings = 0;
  for (const [state, detail] of grades) {
    console.log(`${state.padEnd(20)} ${detail}`);
    if (state === 'file-near-cap') {
      for (const row of big.slice(0, 5)) {
        console.log(`${''.padEnd(20)} ${row.id}  ${human(row.size)}  ${row.purpose}`);
      }
    }
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }
  return findings;
}

function args(argv) {
  const out = { provider: 'both', quotaBytes: 0, fileCapBytes: 0, staleDays: 90,
                maxPages: 50 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--provider') out.provider = argv[i += 1];
    else if (argv[i] === '--quota-bytes') out.quotaBytes = Number(argv[i += 1]);
    else if (argv[i] === '--file-cap-bytes') out.fileCapBytes = Number(argv[i += 1]);
    else if (argv[i] === '--stale-days') out.staleDays = Number(argv[i += 1]);
    else if (argv[i] === '--max-pages') out.maxPages = Number(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const now = Math.trunc(Date.now() / 1000);
  const wanted = opts.provider === 'both' ? ['openai', 'anthropic'] : [opts.provider];
  let ran = 0;
  let findings = 0;
  for (const provider of wanted) {
    const key = process.env[KEY_ENV[provider]];
    if (!key) {
      console.log(`${'not-audited'.padEnd(20)} ${KEY_ENV[provider]} not set, so that `
        + 'store was not audited. An unaudited store is not an empty one');
      continue;
    }
    const [rows, pages, complete] = provider === 'openai'
      ? await fetchOpenai(key, opts.maxPages)
      : await fetchAnthropic(key, opts.maxPages);
    findings += report(provider, rows, pages, complete, opts, now);
    ran += 1;
  }
  if (!ran) {
    console.error('set OPENAI_API_KEY (a project read key) or ANTHROPIC_API_KEY '
      + '(a key with access to the workspace). Every call is a GET of /v1/files');
    process.exitCode = 2;
    return;
  }
  console.log(`${ran} store(s) audited, ${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
