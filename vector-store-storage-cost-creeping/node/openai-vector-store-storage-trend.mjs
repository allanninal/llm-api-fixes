/**
 * Trend retained vector store bytes against the queries that justify them.
 *
 * Read only. Three paged GETs against /v1/organization/* with an admin key,
 * plus one optional GET of /v1/vector_stores with a project key. No request
 * body is constructed and no file_search query is ever run.
 *
 * Storage is a stock rather than a flow, so the finding is a slope rather than
 * a share, and only when the slope is not matched by query volume.
 *
 * The vector stores usage endpoint groups by project_id and nothing else, so
 * naming an individual store needs the snapshot joined to per-store query
 * counts from the file search calls report.
 */
const API = 'https://api.openai.com/v1';
const BETA = { 'OpenAI-Beta': 'assistants=v2' };

/** Rows the report could not attribute. Never folded into a real id. */
export const UNGROUPED = 'ungrouped';

/** The unit that identifies storage on the cost report. Not a name match. */
export const STORAGE_UNIT = 'gibibyte_hours';

const GIB = 1073741824;
const DAY = 86400;

const FINDINGS = new Set(['bytes-growing-queries-flat',
                          'bytes-growing-never-queried']);

const num = (n) => Number(n).toLocaleString('en-US');

/** {projectId: [[startTime, usageBytes]]} sorted by time. Pure. */
export function byteSeries(buckets) {
  const rows = {};
  for (const bucket of buckets ?? []) {
    const start = Math.trunc(Number(bucket?.start_time ?? 0));
    for (const result of bucket?.results ?? []) {
      const key = String(result?.project_id ?? UNGROUPED);
      const value = Number(result?.usage_bytes ?? 0);
      if (!Number.isFinite(value)) continue;
      (rows[key] ??= []).push([start, Math.trunc(value)]);
    }
  }
  for (const points of Object.values(rows)) points.sort((a, b) => a[0] - b[0]);
  return rows;
}

/** {projectId: [[startTime, numRequests]]} sorted by time. Pure. */
export function querySeries(buckets) {
  const rows = {};
  for (const bucket of buckets ?? []) {
    const start = Math.trunc(Number(bucket?.start_time ?? 0));
    for (const result of bucket?.results ?? []) {
      const key = String(result?.project_id ?? UNGROUPED);
      const value = Number(result?.num_requests ?? 0);
      if (!Number.isFinite(value)) continue;
      (rows[key] ??= []).push([start, Math.trunc(value)]);
    }
  }
  for (const points of Object.values(rows)) points.sort((a, b) => a[0] - b[0]);
  return rows;
}

/** {vectorStoreId: total numRequests}. Pure. The only per-store number there is. */
export function searchesByStore(buckets) {
  const rows = {};
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const key = String(result?.vector_store_id ?? UNGROUPED);
      const value = Number(result?.num_requests ?? 0);
      if (!Number.isFinite(value)) continue;
      rows[key] = (rows[key] ?? 0) + Math.trunc(value);
    }
  }
  return rows;
}

/** Least-squares trend in units per day. Pure. Zero on fewer than 2 points. */
export function slope(points) {
  const rows = [...(points ?? [])].sort((a, b) => a[0] - b[0]);
  if (rows.length < 2) return 0;
  const base = rows[0][0];
  const xs = rows.map(([t]) => (t - base) / DAY);
  const ys = rows.map(([, v]) => Number(v));
  const n = rows.length;
  const mx = xs.reduce((a, x) => a + x, 0) / n;
  const my = ys.reduce((a, y) => a + y, 0) / n;
  const denom = xs.reduce((a, x) => a + (x - mx) ** 2, 0);
  if (denom <= 0) return 0;
  let cov = 0;
  for (let i = 0; i < n; i += 1) cov += (xs[i] - mx) * (ys[i] - my);
  return cov / denom;
}

/** [first, last, delta, fraction] over a series. Pure. */
export function growth(points) {
  const rows = [...(points ?? [])].sort((a, b) => a[0] - b[0]);
  if (!rows.length) return [0, 0, 0, 0];
  const first = rows[0][1];
  const last = rows[rows.length - 1][1];
  const delta = last - first;
  return [first, last, delta, first > 0 ? delta / first : 0];
}

/** {lineItem: {dollars, gibibyte_hours}} for storage only. Pure. */
export function storageLines(buckets) {
  const rows = {};
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      if (String(result?.quantity_unit ?? '') !== STORAGE_UNIT) continue;
      const name = String(result?.line_item ?? 'unlabelled');
      const dollars = Number(result?.amount?.value ?? 0);
      const quantity = Number(result?.quantity ?? 0);
      const entry = (rows[name] ??= { dollars: 0, [STORAGE_UNIT]: 0 });
      if (Number.isFinite(dollars)) entry.dollars += dollars;
      if (Number.isFinite(quantity)) entry[STORAGE_UNIT] += quantity;
    }
  }
  return rows;
}

/** [[id, name, bytes, idleDays]] for stores nothing searched. Pure. */
export function idleStores(stores, searches, now, minBytes = GIB) {
  const out = [];
  for (const store of stores ?? []) {
    const sid = String(store?.id ?? '');
    const size = Number(store?.usage_bytes ?? 0);
    if (!sid || !Number.isFinite(size) || size < minBytes) continue;
    if (Number((searches ?? {})[sid] ?? 0) > 0) continue;
    const last = Number(store?.last_active_at ?? 0);
    const idle = Number.isFinite(last) && last > 0
      ? Math.trunc((now - last) / DAY) : -1;
    out.push([sid, String(store?.name ?? '(unnamed)'), Math.trunc(size), idle]);
  }
  out.sort((a, b) => (b[2] - a[2]) || a[0].localeCompare(b[0]));
  return out;
}

/** Classify one project. Pure. Returns [state, detail]. */
export function verdict(bytesPoints, queryPoints, days, minGib = 1.0,
                        minGrowth = 0.25) {
  const [first, last, , fraction] = growth(bytesPoints);
  const queries = (queryPoints ?? []).reduce((a, [, v]) => a + v, 0);

  if (last < minGib * GIB) {
    return ['below-threshold',
            `${(last / GIB).toFixed(1)} GiB, under the ${minGib.toFixed(1)} GiB floor`];
  }
  if (fraction < minGrowth) {
    return ['flat',
            `${(last / GIB).toFixed(1)} GiB, ${fraction >= 0 ? '+' : ''}`
            + `${(fraction * 100).toFixed(0)}% over ${days} day(s), `
            + `${num(queries)} file search call(s)`];
  }

  const shape = `${(first / GIB).toFixed(1)} GiB -> ${(last / GIB).toFixed(1)} GiB `
    + `(${fraction >= 0 ? '+' : ''}${(fraction * 100).toFixed(0)}%)`;
  if (queries <= 0) {
    return ['bytes-growing-never-queried',
            `${shape}, 0 file search call(s) in ${days} day(s)`];
  }
  if (slope(queryPoints) <= 0) {
    return ['bytes-growing-queries-flat',
            `${shape} while file search calls are flat or falling across the same window`];
  }
  return ['bytes-and-queries-growing',
          `${shape}, ${num(queries)} file search call(s). Growth, priced correctly.`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, idle = []) {
  const rows = [...(idle ?? [])];
  if (FINDINGS.has(state)) {
    const lines = [];
    if (state === 'bytes-growing-never-queried') {
      lines.push("no query has touched this project's stores in the window. The "
        + 'bytes are being retained, not used.');
    } else {
      lines.push('the corpus is growing and the query volume is not, so you are '
        + 'paying more each month for the same amount of retrieval.');
    }
    if (rows.length) {
      lines.push('idle stores holding real bytes: ' + rows.slice(0, 8)
        .map(([sid, name, size, days]) => `${sid} ${name} ${(size / GIB).toFixed(1)} GiB`
          + (days < 0 ? '' : `, last active ${days} day(s) ago`)).join('; '));
    } else {
      lines.push('no per-store snapshot was read, so the project is named and the '
        + 'store is not. Add a project key to join the query counts against '
        + 'GET /v1/vector_stores.');
    }
    lines.push('delete the dead ones with DELETE /v1/vector_stores/{vector_store_id} '
      + 'after archiving anything you still need.');
    lines.push('set an expiration policy at creation on stores that are meant to be '
      + 'temporary, so the next prototype ages out on its own rather than being '
      + "somebody's future ticket.");
    return lines;
  }
  if (state === 'bytes-and-queries-growing') {
    return ['nothing to do. This is a corpus that is being used more, and the '
      + 'storage line is supposed to follow it.'];
  }
  return [];
}

/** Unix seconds at midnight UTC, `days` ago. Pure given `now`. */
export function windowStart(days, now = new Date()) {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return Math.floor(midnight / 1000) - days * DAY;
}

async function read(key, path, params, extra = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const one of v) url.searchParams.append(k, String(one));
    else url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}`, ...extra } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/organization/* needs an `
                    + 'organization admin key, not a project key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* usageBuckets(key, path, params, maxPages = 40) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q.page = page.next_page;
  }
}

async function* paged(key, path, params, maxPages = 200) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q, BETA);
    const data = page.data ?? [];
    for (const item of data) yield item;
    if (!page.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key; a project '
                  + 'key cannot read /v1/organization/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 90);
  const minGib = Number((process.env.MIN_GIB || "dummy-min-gib") ?? 1);
  const minGrowth = Number((process.env.MIN_GROWTH || "dummy-min-growth") ?? 0.25);
  const common = { start_time: windowStart(days), bucket_width: '1d', limit: 31 };

  const collect = async (path, params) => {
    const out = [];
    for await (const b of usageBuckets(admin, path, params)) out.push(b);
    return out;
  };

  const bytesBuckets = await collect('/organization/usage/vector_stores',
                                     { ...common, group_by: 'project_id' });
  const searchBuckets = await collect('/organization/usage/file_search_calls',
    { ...common, group_by: ['project_id', 'vector_store_id'] });
  const costBuckets = await collect('/organization/costs',
                                    { ...common, group_by: 'line_item' });

  const byProject = byteSeries(bytesBuckets);
  const queries = querySeries(searchBuckets);
  const perStore = searchesByStore(searchBuckets);

  const stores = [];
  if ((process.env.OPENAI_API_KEY || "dummy-openai-api-key")) {
    for await (const st of paged((process.env.OPENAI_API_KEY || "dummy-openai-api-key"), '/vector_stores',
                                 { limit: 100 })) stores.push(st);
  }

  console.log(`${days} day(s) of daily buckets across `
              + `${Object.keys(byProject).length} project(s), ${stores.length} `
              + 'store(s) in the snapshot');

  const lines = storageLines(costBuckets);
  const dollars = Object.values(lines).reduce((a, v) => a + v.dollars, 0);
  const hours = Object.values(lines).reduce((a, v) => a + v[STORAGE_UNIT], 0);
  if (Object.keys(lines).length) {
    console.log(`storage cost in the window: $${dollars.toFixed(2)} over `
                + `${hours.toFixed(1)} ${STORAGE_UNIT}`);
  } else {
    console.log(`no cost result carried quantity_unit '${STORAGE_UNIT}' in the `
                + 'window, so nothing is being billed for storage yet');
  }

  const now = Math.floor(Date.now() / 1000);
  const idle = idleStores(stores, perStore, now, Math.trunc(minGib * GIB));

  let findings = 0;
  for (const project of Object.keys(byProject).sort()) {
    const [state, detail] = verdict(byProject[project], queries[project] ?? [],
                                    days, minGib, minGrowth);
    console.log(`${state.padEnd(27)} ${project}: ${detail}`);
    for (const line of repairLines(state, FINDINGS.has(state) ? idle : [])) {
      console.log(`  repair: ${line}`);
    }
    if (FINDINGS.has(state)) findings += 1;
  }

  if (perStore[UNGROUPED]) {
    console.log(`${num(perStore[UNGROUPED])} file search call(s) came back with no `
                + 'vector_store_id and are not attributed to a store');
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
