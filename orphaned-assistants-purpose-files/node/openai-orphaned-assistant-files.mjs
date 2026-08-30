/**
 * Subtract the ids surviving vector stores hold from one dead purpose class.
 *
 * Read only. Four kinds of GET: two file listings, the vector store listing,
 * and one file listing per store. Nothing is created and nothing is deleted.
 *
 * The Assistants API reached its shutdown date on 2026-08-26. Its objects
 * went; the files they referenced did not. `assistants` and
 * `assistants_output` are still valid values of `purpose` on the File object.
 *
 * The subtraction is only as good as the set being subtracted, so a store
 * whose file listing could not be read downgrades every verdict in the run.
 */
const FILES_URL = 'https://api.openai.com/v1/files';
const STORES_URL = 'https://api.openai.com/v1/vector_stores';

export const PURPOSES = ['assistants', 'assistants_output'];
const FINDINGS = new Set(['orphan', 'orphan-output', 'subtraction-incomplete']);

// The files listing accepts up to 10,000 per page; both vector store listings
// cap at 100. Copying the first onto the second silently truncates the set.
const FILE_PAGE = 10000;
const STORE_PAGE = 100;

/** One file object, reduced. Pure. */
export function fileRow(body) {
  const row = (body && typeof body === 'object') ? body : {};
  const size = Number(row.bytes);
  const created = Number(row.created_at ?? 0);
  return {
    id: String(row.id ?? ''),
    filename: String(row.filename ?? ''),
    size: Number.isFinite(size) ? Math.max(0, Math.trunc(size)) : 0,
    purpose: String(row.purpose ?? ''),
    created_at: Number.isFinite(created) ? Math.max(0, Math.trunc(created)) : 0,
  };
}

/** The set to subtract. Pure. A store file's own id is the Files API id. */
export function referencedIds(storeFiles) {
  const out = new Set();
  for (const item of storeFiles ?? []) {
    if (item && typeof item === 'object') {
      const id = String(item.id ?? '');
      if (id) out.add(id);
    }
  }
  return out;
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

/** Age in days. Pure. The clock is an argument. Null when undatable. */
export function ageDays(createdAt, now) {
  const created = Number(createdAt);
  const at = Number(now);
  if (!Number.isFinite(created) || !Number.isFinite(at) || created <= 0) return null;
  return (at - created) / 86400;
}

/** Grade the purpose class as a whole. Pure. */
export function classState(rows, complete) {
  if (!complete) {
    return ['subtraction-unsafe',
      'the referenced set is incomplete, so no file in this class can be called '
      + 'an orphan'];
  }
  if (!(rows ?? []).length) {
    return ['class-empty',
      'no file carries purpose assistants or assistants_output, so nothing was '
      + 'left behind here'];
  }
  return ['class-populated',
    `${rows.length} file(s) carry a purpose whose owning API no longer exists`];
}

/** Grade one file. Pure. Completeness is tested before anything else. */
export function classifyFile(row, referenced, complete, now) {
  const file = (row && typeof row === 'object') ? row : {};
  const id = String(file.id ?? '');
  if (!complete) {
    return ['subtraction-incomplete',
      `${id}: at least one vector store could not be listed, so this file cannot `
      + 'be called an orphan'];
  }
  if ((referenced ?? new Set()).has(id)) {
    return ['still-referenced',
      `${id}: held by a live vector store, so file search under the Responses API `
      + 'still reads it'];
  }
  const age = ageDays(file.created_at, now);
  const when = age === null ? 'undated' : `created ${age.toFixed(0)} day(s) ago`;
  if (file.purpose === 'assistants_output') {
    return ['orphan-output',
      `${id}: code interpreter output from a run that no longer exists, `
      + `${human(file.size)}, ${when}`];
  }
  return ['orphan',
    `${id}: no surviving vector store holds this id, ${human(file.size)}, ${when}`];
}

/** Fold graded rows into per-state counts and bytes. Pure. */
export function summarise(graded) {
  const acc = {};
  for (const [state, row] of graded ?? []) {
    const cur = acc[state] ?? { count: 0, bytes: 0 };
    cur.count += 1;
    cur.bytes += Number(row?.size ?? 0);
    acc[state] = cur;
  }
  return acc;
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, orphanCount = 0, orphanBytes = 0, unreadable = []) {
  if (state === 'orphan' || state === 'orphan-output') {
    return [`${orphanCount} confirmed orphan(s), ${human(orphanBytes)}. Archive `
      + 'anything you still want, then DELETE /v1/files/{file_id} one at a time. '
      + 'The delete also removes the file from every vector store holding it.',
    're-upload future file search sources with purpose user_data and an '
      + 'expires_after policy, so the next class ages out on its own.'];
  }
  if (state === 'subtraction-incomplete' || state === 'subtraction-unsafe') {
    return [`${(unreadable ?? []).length} vector store(s) could not be listed: `
      + `${[...(unreadable ?? [])].sort().join(', ') || 'unknown'}. Re-run with a `
      + 'key that can read them. A set difference against an incomplete set '
      + 'names files that are perfectly well referenced.'];
  }
  return [];
}

async function getPage(url, params, key) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) {
    target.searchParams.set(k, String(v));
  }
  try {
    const res = await fetch(target, { headers: { Authorization: `Bearer ${key}` } });
    if (res.status !== 200) return [null, false];
    return [await res.json().catch(() => null), true];
  } catch {
    return [null, false];
  }
}

async function walk(url, key, params, pageSize, maxPages) {
  const items = [];
  let cursor = null;
  let pages = 0;
  while (pages < maxPages) {
    const query = { ...(params ?? {}), limit: pageSize };
    if (cursor) query.after = cursor;
    const [body, ok] = await getPage(url, query, key);
    if (!ok) return [items, false];
    const data = (body ?? {}).data ?? [];
    pages += 1;
    items.push(...data);
    if (!data.length || (body ?? {}).has_more === false) return [items, true];
    if (!('has_more' in (body ?? {})) && data.length < pageSize) return [items, true];
    cursor = data[data.length - 1]?.id;
    if (!cursor) return [items, true];
  }
  return [items, false];
}

function args(argv) {
  const out = { maxPages: 50, show: 25 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--max-pages') out.maxPages = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--show') out.show = Number.parseInt(argv[i += 1], 10);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only. Every '
      + 'call is a GET of /v1/files or /v1/vector_stores');
    process.exitCode = 2;
    return;
  }

  const now = Math.trunc(Date.now() / 1000);
  const rows = [];
  for (const purpose of PURPOSES) {
    const [items, ok] = await walk(FILES_URL, key, { purpose, order: 'asc' },
                                   FILE_PAGE, opts.maxPages);
    if (!ok) {
      console.error(`the ${purpose} listing could not be read in full; nothing `
        + 'can be concluded from a partial class');
      process.exitCode = 2;
      return;
    }
    for (const item of items) rows.push(fileRow(item));
  }

  const [stores, storesOk] = await walk(STORES_URL, key, {}, STORE_PAGE, opts.maxPages);
  const referenced = new Set();
  const unreadable = [];
  for (const store of stores) {
    const id = String(store?.id ?? '');
    if (!id) continue;
    const [items, ok] = await walk(`${STORES_URL}/${id}/files`, key, {},
                                   STORE_PAGE, opts.maxPages);
    for (const fid of referencedIds(items)) referenced.add(fid);
    if (!ok) unreadable.push(id);
  }
  const complete = storesOk && !unreadable.length;

  console.log(`${stores.length} vector store(s) read, ${referenced.size} `
    + 'referenced file id(s)');
  const counts = Object.fromEntries(PURPOSES.map((p) =>
    [p, rows.filter((r) => r.purpose === p).length]));
  console.log(`${rows.length} file(s) in the class: ${counts.assistants} assistants, `
    + `${counts.assistants_output} assistants_output, `
    + `${human(rows.reduce((a, r) => a + r.size, 0))}`);
  console.log('  measured: two purpose listings, minus the ids held by every store read');
  console.log('  inferred: that a file in no surviving store has no owner. The '
    + 'migration guide documents nothing at all about files or vector stores');
  if (!storesOk) console.log('  the vector store listing itself was truncated or failed');

  const [state, detail] = classState(rows, complete);
  console.log(`${state.padEnd(20)} ${detail}`);

  const graded = rows.map((row) => [classifyFile(row, referenced, complete, now)[0], row]);
  let shown = 0;
  for (const row of rows) {
    const [verdict, line] = classifyFile(row, referenced, complete, now);
    if (shown < opts.show) {
      console.log(`${verdict.padEnd(20)} ${line}`);
      shown += 1;
    }
  }

  const tot = summarise(graded);
  const orphans = tot.orphan ?? { count: 0, bytes: 0 };
  const outputs = tot['orphan-output'] ?? { count: 0, bytes: 0 };
  let findings = 0;
  for (const s of FINDINGS) findings += tot[s]?.count ?? 0;
  if (orphans.count || outputs.count) {
    for (const line of repairLines('orphan', orphans.count + outputs.count,
                                   orphans.bytes + outputs.bytes)) {
      console.log(`  repair: ${line}`);
    }
  }
  if (!complete) {
    for (const line of repairLines('subtraction-incomplete', 0, 0, unreadable)) {
      console.log(`  repair: ${line}`);
    }
  }
  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
