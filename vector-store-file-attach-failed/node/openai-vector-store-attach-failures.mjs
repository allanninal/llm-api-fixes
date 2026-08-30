/**
 * Find files that never indexed in an OpenAI vector store.
 *
 * Read only. Every request is a GET. No request body is constructed and no
 * file_search query is ever run, because a retrieval query is a generation and
 * a script about a broken index should not create traffic against it.
 *
 * The subject is the child object: a vector_store.file carries last_error.code
 * with one of exactly three values, while the parent's status becomes
 * "completed" when nothing is pending whether or not anything succeeded.
 */
const API = 'https://api.openai.com/v1';

// The official client still sends this on every vector store call.
const BETA = { 'OpenAI-Beta': 'assistants=v2' };

export const ERROR_CODES = ['server_error', 'unsupported_file', 'invalid_file'];

// A failed child whose last_error is null. Nullable on every child, and a
// reader that keys on last_error.code drops exactly the rows nobody has read.
export const UNREPORTED = 'unreported';

const REPAIRS = {
  unsupported_file:
    'unsupported_file is a format the parser cannot read: a scan with no text '
    + 'layer, or an extension it does not handle. OCR the scans and export the '
    + 'rest to .md or .txt, then attach again.',
  invalid_file:
    'invalid_file is usually empty, corrupt or password protected. Fix it at '
    + 'the source; re-attaching the same bytes fails the same way.',
  server_error:
    'server_error is transient. Attach those files again and re-check before '
    + 'treating them as a content problem.',
  [UNREPORTED]:
    'these failed with no last_error at all. Fetch each one with GET '
    + '/v1/vector_stores/{vector_store_id}/files/{file_id} before deciding, '
    + 'because a failure with no stated reason has not been looked at.',
};

const FINDINGS = new Set(['attach-failed', 'ingestion-stalled', 'counts-disagree']);

/** The five file_counts integers, coerced. Pure. */
export function counts(store) {
  const raw = store?.file_counts ?? {};
  const out = {};
  for (const key of ['in_progress', 'completed', 'failed', 'cancelled', 'total']) {
    const n = Number(raw[key] ?? 0);
    out[key] = Number.isFinite(n) ? Math.trunc(n) : 0;
  }
  return out;
}

/** {code: [fileId]} over the failed children. Pure. */
export function bucketErrors(files) {
  const out = {};
  for (const entry of files ?? []) {
    const row = entry ?? {};
    if (String(row.status ?? '').trim().toLowerCase() !== 'failed') continue;
    const code = String(row.last_error?.code ?? '').trim().toLowerCase() || UNREPORTED;
    (out[code] ??= []).push(String(row.id ?? '?'));
  }
  for (const ids of Object.values(out)) ids.sort();
  return out;
}

/** [[fileId, ageSeconds]] for children pinned in_progress. Pure. Oldest first. */
export function stalled(files, now, maxAge = 3600) {
  const out = [];
  for (const entry of files ?? []) {
    const row = entry ?? {};
    if (String(row.status ?? '').trim().toLowerCase() !== 'in_progress') continue;
    const created = Number(row.created_at ?? 0);
    if (!Number.isFinite(created) || created <= 0) continue;
    if (now - created > maxAge) out.push([String(row.id ?? '?'), Math.trunc(now - created)]);
  }
  out.sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
  return out;
}

/** failed / total. Pure. Zero on an empty store rather than a division by zero. */
export function failureRate(c) {
  const total = Number(c?.total ?? 0);
  if (!(total > 0)) return 0;
  return Number(c?.failed ?? 0) / total;
}

/** [claimed, listed] failure counts. Pure. Two numbers, never one. */
export function reconcile(c, buckets) {
  const listed = Object.values(buckets ?? {}).reduce((a, v) => a + v.length, 0);
  const claimed = Number(c?.failed ?? 0);
  return [Number.isFinite(claimed) ? Math.trunc(claimed) : 0, listed];
}

/** Classify one store. Pure. Returns [state, detail]. */
export function verdict(c, buckets, stalledRows) {
  const cc = c ?? {};
  const total = Math.trunc(Number(cc.total ?? 0));
  const [claimed, listed] = reconcile(cc, buckets);
  const rows = [...(stalledRows ?? [])];

  if (total <= 0) {
    return ['no-files',
            'nothing has ever been attached, so this is the empty vector store '
            + 'note rather than this one'];
  }
  if (listed > 0) {
    let detail = `${listed} of ${total} file(s) failed `
      + `(${(failureRate(cc) * 100).toFixed(1)}%)`;
    if (claimed !== listed) {
      detail += ` -- file_counts.failed says ${claimed} and the listing returns `
        + `${listed}, so read the listing`;
    }
    return ['attach-failed', detail];
  }
  if (claimed > 0) {
    return ['counts-disagree',
            `file_counts.failed is ${claimed} and the filtered listing returns `
            + 'none, which is what a half-finished repair looks like: the failed '
            + 'files were detached and never attached again'];
  }
  if (rows.length) {
    const oldest = Math.max(Math.trunc(rows[0][1] / 3600), 1);
    return ['ingestion-stalled',
            `${rows.length} file(s) still in_progress, the oldest for over `
            + `${oldest}h. The parent stays in_progress while any child is.`];
  }
  if (Math.trunc(Number(cc.in_progress ?? 0)) > 0) {
    return ['still-ingesting',
            `${Math.trunc(Number(cc.in_progress))} file(s) in_progress and none of `
            + 'them old enough to call pinned. Re-run after the ingest settles.'];
  }
  return ['complete',
          `${total} file(s), all completed, and the summary agrees with the listing`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, buckets = {}, stalledRows = []) {
  const b = buckets ?? {};
  if (state === 'attach-failed') {
    const ordered = Object.keys(b).sort(
      (x, y) => (b[y].length - b[x].length) || x.localeCompare(y));
    const lines = ordered.filter((code) => REPAIRS[code]).map((code) => REPAIRS[code]);
    const unknown = ordered.filter((code) => !REPAIRS[code]);
    if (unknown.length) {
      lines.push(`last_error.code came back as ${unknown.join(', ')}, which is not `
        + 'one of the three documented values. Read the message field before '
        + 'acting on it.');
    }
    lines.push('gate the ingest job on file_counts.failed == 0, not on '
      + 'status == "completed", which only means nothing is pending.');
    return lines;
  }
  if (state === 'counts-disagree') {
    return [
      "list the store's files without a filter and compare the ids against your "
      + 'ingest manifest. The failures are gone from the store and are still '
      + 'missing from retrieval.',
      're-attach the manifest entries that no longer appear, then assert '
      + 'file_counts.failed == 0 and file_counts.completed == file_counts.total '
      + 'before declaring the store ready.',
    ];
  }
  if (state === 'ingestion-stalled') {
    const oldest = [...(stalledRows ?? [])].slice(0, 5);
    const lines = ['detach and attach those files again rather than waiting. A '
      + 'child pinned for hours is not going to finish on its own.'];
    if (oldest.length) {
      lines.push('oldest pinned: ' + oldest
        .map(([id, age]) => `${id} (${Math.trunc(age / 3600)}h)`).join(', '));
    }
    lines.push('stagger large ingests, and poll file_counts.in_progress down to '
      + 'zero with a timeout rather than assuming that attach means indexed.');
    return lines;
  }
  if (state === 'no-files') {
    return ['an empty store fails differently and is repaired differently. '
      + 'Re-run the ingest, or stop naming the store in vector_store_ids.'];
  }
  return [];
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}`, ...BETA } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/vector_stores needs a project key `
                    + 'for the project that owns the stores');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* paged(key, path, params, maxPages = 200) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q);
    const data = page.data ?? [];
    for (const item of data) yield item;
    if (!page.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function collect(key, path, params) {
  const out = [];
  for await (const item of paged(key, path, params)) out.push(item);
  return out;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key for the project that owns '
                  + 'the vector stores');
    process.exitCode = 2;
    return;
  }
  const maxAge = Math.trunc(Number((process.env.STALLED_HOURS || "dummy-stalled-hours") ?? 1) * 3600);
  const wanted = new Set(((process.env.VECTOR_STORE_IDS || "dummy-vector-store-ids") ?? '')
    .split(/[,\s]+/).filter(Boolean));

  let stores = await collect(key, '/vector_stores', { limit: 100 });
  if (wanted.size) stores = stores.filter((st) => wanted.has(st?.id));
  console.log(`${stores.length} store(s) visible to this key`);

  const now = Math.floor(Date.now() / 1000);
  let findings = 0;

  for (const store of stores) {
    const sid = store?.id ?? '?';
    const name = store?.name ?? '(unnamed)';
    const c = counts(store);

    let failed = [];
    let pending = [];
    if (c.total > 0) {
      failed = await collect(key, `/vector_stores/${sid}/files`,
                             { limit: 100, filter: 'failed' });
      if (c.in_progress > 0) {
        pending = await collect(key, `/vector_stores/${sid}/files`,
                                { limit: 100, filter: 'in_progress' });
      }
    }

    const buckets = bucketErrors(failed);
    const stalledRows = stalled(pending, now, maxAge);
    const [state, detail] = verdict(c, buckets, stalledRows);

    console.log(`${state.padEnd(20)} ${sid} ${name}: ${detail}`);
    if (state === 'attach-failed') {
      const ordered = Object.keys(buckets).sort(
        (x, y) => (buckets[y].length - buckets[x].length) || x.localeCompare(y));
      for (const code of ordered) {
        const ids = buckets[code];
        const shown = ids.slice(0, 3).join(', ') + (ids.length > 3 ? ' ...' : '');
        console.log(`  ${code.padEnd(18)} ${ids.length} file(s)  ${shown}`);
      }
    }
    for (const line of repairLines(state, buckets, stalledRows)) {
      console.log(`  repair: ${line}`);
    }
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
