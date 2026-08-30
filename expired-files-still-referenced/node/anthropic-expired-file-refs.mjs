/**
 * Check the file ids an application holds against the expiry on each one.
 *
 * Read only. GET /v1/files with an ids[] filter and nothing else. No file
 * content is ever downloaded, nothing is uploaded and nothing is deleted.
 *
 * expires_in_seconds is set once at upload and cannot be changed. After
 * expires_at the content stops being retrievable and the bytes leave the
 * storage quota, while the metadata remains readable for up to 30 days.
 *
 * The ids form accepts at most 100 values after de-duplication, is mutually
 * exclusive with page and limit, and silently omits ids that do not resolve.
 *
 * No anthropic-beta header is sent: with files-api-2025-04-14 the response
 * omits expires_at entirely and this check cannot run.
 */
import { readFile } from 'node:fs/promises';

const BASE_URL = 'https://api.anthropic.com/v1/files';

export const ID_BATCH = 100;
export const METADATA_WINDOW_DAYS = 30;

const FINDINGS = new Set(['expired', 'expiring', 'gone', 'expiry-not-reported']);

/** File ids from an export nobody tidied. Pure. */
export function parseIds(text) {
  const seen = [];
  for (const line of String(text ?? '').split('\n')) {
    const item = line.split('#')[0].trim();
    if (item && !seen.includes(item)) seen.push(item);
  }
  return seen;
}

/** Batches of at most `size` unique ids, capped at the documented 100. Pure. */
export function chunks(ids, size = ID_BATCH) {
  let step = Number.parseInt(size, 10);
  if (!Number.isFinite(step)) step = ID_BATCH;
  step = Math.max(1, Math.min(step, ID_BATCH));
  const unique = [];
  for (const raw of ids ?? []) {
    const item = String(raw ?? '').trim();
    if (item && !unique.includes(item)) unique.push(item);
  }
  const out = [];
  for (let i = 0; i < unique.length; i += step) out.push(unique.slice(i, i + step));
  return out;
}

/** RFC 3339 to seconds. Pure. Zero for anything unparseable, never a guess. */
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

/** One file object, reduced. Pure. Absent expires_at is not null expires_at. */
export function fileRow(body) {
  const row = (body && typeof body === 'object') ? body : {};
  const size = Number(row.size_bytes);
  const expires = epoch(row.expires_at);
  return {
    id: String(row.id ?? ''),
    filename: String(row.filename ?? ''),
    size: Number.isFinite(size) ? Math.max(0, Math.trunc(size)) : 0,
    created_at: epoch(row.created_at),
    expires_at: expires || null,
    expiry_reported: Object.prototype.hasOwnProperty.call(row, 'expires_at'),
    downloadable: Boolean(row.downloadable),
  };
}

/** Ids asked for and not answered. Pure. Order preserved. */
export function missingIds(requested, returned) {
  const have = new Set((returned ?? []).map((r) => String(r ?? '')));
  return (requested ?? []).map(String).filter((r) => !have.has(r));
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

/** Grade one referenced id. Pure. `row` is null for an id never returned. */
export function classifyId(row, now, warnDays) {
  if (row === null || row === undefined) {
    return ['gone', `not returned by the ids lookup, so it is past even the `
      + `${METADATA_WINDOW_DAYS} day metadata window or was deleted`];
  }
  if (!row.expiry_reported) {
    return ['expiry-not-reported',
      'the object came back with no expires_at field, so this check could not run'];
  }
  if (!row.expires_at) return ['no-expiry', 'no expiry was set, so this one is permanent'];
  const left = (Number(row.expires_at) - Number(now)) / 86400;
  if (left <= 0) {
    return ['expired', `expired ${Math.abs(left).toFixed(1)} day(s) ago; the `
      + 'metadata still answers and every actual use of this id fails'];
  }
  if (left <= Number(warnDays)) {
    return ['expiring',
      `expires in ${left.toFixed(1)} day(s), and the expiry cannot be extended`];
  }
  return ['live', `live, expires in ${left.toFixed(1)} day(s)`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'expired') {
    return ['the content is gone and cannot be restored. Remove the reference, '
      + 're-upload the source if you still need it, and DELETE /v1/files/{file_id} '
      + `to clear the metadata immediately rather than waiting out the `
      + `${METADATA_WINDOW_DAYS} day window.`];
  }
  if (state === 'expiring') {
    return ['expires_in_seconds is set once at upload and cannot be changed, so '
      + 'there is nothing to extend. Re-upload before the date and swap the id, '
      + 'or upload with no expiry and accept that it stays on the storage quota.'];
  }
  if (state === 'gone') {
    return ['this id resolves to nothing at all. Treat the record as stale and '
      + 'stop passing it, because no read will recover the file.'];
  }
  if (state === 'expiry-not-reported') {
    return ['drop the anthropic-beta: files-api-2025-04-14 header. With it the '
      + 'response omits expires_at entirely and reverts to before_id and after_id '
      + 'paging, so this check cannot run.'];
  }
  if (state === 'no-expiry') {
    return ['nothing to do here, but note that a file with no expiry never leaves '
      + 'the storage total either.'];
  }
  return [];
}

async function fetchBatch(batch, key) {
  const url = new URL(BASE_URL);
  for (const id of batch) url.searchParams.append('ids[]', id);
  const headers = { 'x-api-key': key, 'anthropic-version': '2023-06-01' };
  try {
    const res = await fetch(url, { headers });
    if (res.status !== 200) {
      console.error(`ids lookup returned HTTP ${res.status}`);
      return [[], false];
    }
    const body = await res.json().catch(() => null);
    if (!body) return [[], false];
    return [(body.data ?? []).map(fileRow), true];
  } catch (err) {
    console.error(`ids lookup failed: ${err.message}`);
    return [[], false];
  }
}

function args(argv) {
  const out = { warnDays: 7 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--ids') out.ids = argv[i += 1];
    else if (argv[i] === '--warn-days') out.warnDays = Number(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a key with access to the workspace '
      + 'that owns these files. Every call is a GET of /v1/files');
    process.exitCode = 2;
    return;
  }
  if (!opts.ids) {
    console.error('usage: --ids <file> [--warn-days 7]');
    process.exitCode = 2;
    return;
  }
  let wanted;
  try {
    wanted = parseIds(await readFile(opts.ids, 'utf8'));
  } catch (err) {
    console.error(`could not read ${opts.ids}: ${err.message}`);
    process.exitCode = 2;
    return;
  }
  if (!wanted.length) {
    console.error(`no file ids in ${opts.ids}. This note is about the ids your `
      + 'application holds, not about the workspace listing');
    process.exitCode = 2;
    return;
  }

  const now = Math.trunc(Date.now() / 1000);
  const batches = chunks(wanted);
  const rows = [];
  const missing = [];
  for (const batch of batches) {
    const [got, ok] = await fetchBatch(batch, key);
    if (!ok) {
      console.error('a batch could not be read, so nothing is concluded');
      process.exitCode = 2;
      return;
    }
    rows.push(...got);
    missing.push(...missingIds(batch, got.map((r) => r.id)));
  }

  console.log(`${wanted.length} id(s) asked in ${batches.length} batch(es) of at `
    + `most ${ID_BATCH}, ${rows.length} returned`);

  let findings = 0;
  for (const row of rows) {
    const [state, detail] = classifyId(row, now, opts.warnDays);
    console.log(`${state.padEnd(20)} ${row.id}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }
  for (const id of missing) {
    const [state, detail] = classifyId(null, now, opts.warnDays);
    console.log(`${state.padEnd(20)} ${id}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    findings += 1;
  }

  console.log(`${missing.length} id(s) missing from the response, ${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
