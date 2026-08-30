/**
 * Report OpenAI batches that expired, and the ones about to.
 *
 * Read only. GET requests and nothing else: give this a project key set to Read
 * Only. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// completion_window accepts one value. This is not a default, it is the value.
const WINDOW = 86400;

const IN_FLIGHT = ['validating', 'in_progress', 'finalizing', 'cancelling'];

// Terminal and not this note.
const SETTLED = ['completed', 'failed', 'cancelled'];

/** Read request_counts into [total, completed], or null. Pure. */
export function countsOf(batch) {
  const counts = batch.request_counts;
  if (counts === null || typeof counts !== 'object' || Array.isArray(counts)) return null;
  const total = Number(counts.total ?? 0);
  const done = Number(counts.completed ?? 0);
  if (!Number.isFinite(total) || !Number.isFinite(done)) return null;
  return [Math.trunc(total), Math.trunc(done)];
}

/**
 * When this batch's window closes, and where the number came from. Pure.
 * Returns [unixSeconds, source] or [null, reason]. Three timestamps can answer
 * this and they are not equally good, which is why the source is returned
 * alongside the number: expires_at is the API's own answer, in_progress_at plus
 * 24h is the deadline when it is absent, and created_at plus 24h is an upper
 * bound only, because time spent validating is not part of the window.
 */
export function deadline(batch) {
  const candidates = [
    ['expires_at', 0, 'expires_at'],
    ['in_progress_at', WINDOW, 'in_progress_at plus 24h'],
    ['created_at', WINDOW,
      'created_at plus 24h, an upper bound: the window starts when the batch ' +
      'starts processing, not when it was created'],
  ];
  for (const [field, offset, source] of candidates) {
    const raw = batch[field];
    if (raw === null || raw === undefined || raw === '') continue;
    const value = Number(raw);
    if (Number.isFinite(value) && value > 0) return [Math.trunc(value) + offset, source];
  }
  return [null, 'no usable timestamp on this object'];
}

/**
 * Classify one object from GET /v1/batches against a clock you pass in. Pure.
 * warnHours is the headroom below which an in-flight batch is called out: 4
 * hours left of a 24 hour window is the 20 hour mark. Returns [state, detail].
 */
export function verdict(batch, now, warnHours = 4) {
  const status = String(batch.status ?? '').trim().toLowerCase();
  const numbers = countsOf(batch);
  const [total, done] = numbers ?? [0, 0];
  const rows = total ? `${done} of ${total} row(s)` : 'an unreadable count of rows';

  if (status === 'expired') {
    const missing = Math.max(0, total - done);
    return ['expired',
      `the 24 hour window closed with ${missing} row(s) unfinished (${rows} ` +
      'done). Each one is a batch_expired line in the error file, and none of ' +
      'them will run.'];
  }
  if (SETTLED.includes(status)) {
    return ['settled', `status is ${status}, so no window is running against it`];
  }
  if (!IN_FLIGHT.includes(status)) {
    return ['unreadable',
      `status is ${JSON.stringify(status || null)}, which is not a lifecycle ` +
      'state this script recognises'];
  }

  const [when, source] = deadline(batch);
  if (when === null) {
    return ['unreadable',
      `still ${status} and there is ${source}, so the window cannot be measured`];
  }

  const left = when - Math.trunc(now);
  const hours = (Math.abs(left) / 3600).toFixed(1);
  if (left <= 0) {
    return ['overdue',
      `still ${status}, ${hours} hour(s) past the close of its window (from ` +
      `${source}). The rows that have not run are not going to.`];
  }
  if (left <= warnHours * 3600) {
    return ['expiring-soon',
      `${hours} hour(s) of window left (from ${source}) with ${rows} done. ` +
      'Submit the tail as a second batch while there is still time.'];
  }
  return ['in-flight',
    `${hours} hour(s) of window left (from ${source}); ${rows} done`];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: the key is wrong, revoked, or belongs to ' +
                    'another project');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* walk(key, pageSize, maxPages) {
  let params = { limit: pageSize };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/batches', params);
    const data = page.data ?? [];
    for (const batch of data) yield batch;
    if (!page.has_more || data.length === 0) return;
    params = { limit: pageSize, after: data[data.length - 1].id };
  }
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }

  const warnHours = Number((process.env.WARN_HOURS || "dummy-warn-hours") ?? 4);
  const pageSize = Number((process.env.LIMIT || "dummy-limit") ?? 100);
  const maxPages = Number((process.env.PAGES || "dummy-pages") ?? 20);
  const showAll = process.argv.includes('--show-all');
  const now = Math.floor(Date.now() / 1000);

  let checked = 0;
  let expired = 0;
  let closing = 0;
  for await (const batch of walk(key, pageSize, maxPages)) {
    const [state, detail] = verdict(batch, now, warnHours);
    const line = `${state.padEnd(15)} ${String(batch.id ?? '?')}  ${detail}`;
    checked += 1;

    if (state === 'expired') {
      expired += 1;
      console.warn(line);
      const errorFile = batch.error_file_id;
      console.warn('  repair: rebuild a .jsonl of the custom_ids whose ' +
        'error.code is batch_expired' +
        (errorFile ? ` from GET /v1/files/${errorFile}/content` : '') +
        ' and re-submit them, then split future jobs so one batch stays well ' +
        'under 50,000 requests');
    } else if (state === 'overdue' || state === 'expiring-soon') {
      closing += 1;
      console.warn(line);
      console.warn('  repair: store expires_at in your own job table and alert ' +
        'at the 20 hour mark; a poller that waits for status == completed waits ' +
        'forever on an expired batch');
    } else if (state === 'unreadable') {
      console.warn(line);
    } else if (showAll || state === 'in-flight') {
      console.log(line);
    }
  }

  console.log(`${checked} batch(es) checked, ${expired} expired, ${closing} ` +
              'close to expiring');
  process.exitCode = (expired || closing) ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
