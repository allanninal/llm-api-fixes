/**
 * Report OpenAI batch error files that exist and were never fetched.
 *
 * Read only. Two GET requests and nothing else: give this a project key set to
 * Read Only. The repair is printed, never performed.
 *
 * The API cannot tell you whether you read a file, so the second half of this
 * check comes from you, as a list of error file ids your ingest has consumed.
 */
import { readFileSync } from 'node:fs';

const API = 'https://api.openai.com/v1';
const DAY = 86400;

// Batch input, output and error files are retained for 30 days from creation.
const RETENTION_DAYS = 30;

const IN_FLIGHT = ['validating', 'in_progress', 'finalizing', 'cancelling'];

const FINDINGS = ['unread', 'expiring', 'aged-out'];

/**
 * Whole days of retention left on a file, or null if unreadable. Pure. Floors
 * the elapsed time, so a file created 29.9 days ago has 1 day left rather than
 * 0.1: the number is printed to a human who will act on it tomorrow.
 */
export function daysLeft(createdAt, now, retentionDays = RETENTION_DAYS) {
  const created = Number(createdAt);
  if (!Number.isFinite(created) || created <= 0) return null;
  return retentionDays - Math.floor((Number(now) - created) / DAY);
}

/**
 * Classify one batch against its error file and your ingest record. Pure.
 * fileMeta is the object from GET /v1/files/{id}, or null when that call found
 * nothing. now is unix seconds, passed in so the retention boundary can be
 * tested at a fixed instant. Returns [state, detail].
 */
export function verdict(batch, fileMeta, fetched, now,
                        retentionDays = RETENTION_DAYS, urgentDays = 3) {
  const status = String(batch.status ?? '').trim().toLowerCase();
  const fileId = String(batch.error_file_id ?? '').trim();

  if (IN_FLIGHT.includes(status)) {
    return ['running',
      `status is ${status}; an error file is not final until the batch stops`];
  }
  if (!fileId) {
    return ['no-error-file',
      'no error_file_id on this batch, so nothing failed hard enough to be ' +
      'written to one'];
  }
  const seen = fetched instanceof Set ? fetched : new Set(fetched ?? []);
  if (seen.has(fileId)) {
    return ['fetched',
      `error file ${fileId} is in the ingest record, so the failures were read`];
  }

  const isMeta = fileMeta !== null && typeof fileMeta === 'object';
  const created = (isMeta && fileMeta.created_at) || batch.created_at;
  const left = daysLeft(created, now, retentionDays);

  if (!isMeta) {
    if (left !== null && left <= 0) {
      return ['aged-out',
        `error file ${fileId} is past the ${retentionDays} day retention ` +
        'window and GET /v1/files no longer returns it. Which rows failed, ' +
        'and why, cannot be recovered by any read call now.'];
    }
    return ['unresolvable',
      `the batch names error file ${fileId} but GET /v1/files/${fileId} ` +
      'returned nothing, and the file is still inside the retention window. ' +
      'Check that id by hand.'];
  }

  const raw = Number(fileMeta.bytes ?? 0);
  const size = Number.isFinite(raw) ? Math.trunc(raw) : 0;

  if (size <= 0) {
    return ['empty',
      `error file ${fileId} exists and holds 0 byte(s). The id was allocated ` +
      'and never written to, so there is nothing in it to read.'];
  }
  if (left !== null && left <= 0) {
    return ['aged-out',
      `error file ${fileId} holds ${size} byte(s) that are past the ` +
      `${retentionDays} day retention window. The metadata is still listed; ` +
      'the content is not retrievable.'];
  }
  if (left !== null && left <= urgentDays) {
    return ['expiring',
      `error file ${fileId} holds ${size} byte(s), is not in the ingest ` +
      `record, and expires in ${left} day(s). Download it before the window ` +
      'closes.'];
  }
  return ['unread',
    `error file ${fileId} holds ${size} byte(s) and is not in the ingest ` +
    'record. Every line in it is a row missing from the downstream table.'];
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
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* walk(key, pageSize, maxPages) {
  let params = { limit: pageSize };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/batches', params);
    const data = page?.data ?? [];
    for (const batch of data) yield batch;
    if (!page?.has_more || data.length === 0) return;
    params = { limit: pageSize, after: data[data.length - 1].id };
  }
}

function readFetched() {
  const ids = new Set();
  process.argv.forEach((arg, i) => {
    if (arg === '--fetched' && process.argv[i + 1]) ids.add(process.argv[i + 1]);
    if (arg === '--fetched-file' && process.argv[i + 1]) {
      for (const line of readFileSync(process.argv[i + 1], 'utf8').split('\n')) {
        if (line.trim()) ids.add(line.trim());
      }
    }
  });
  return ids;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }

  const fetched = readFetched();
  if (fetched.size === 0) {
    console.log('no ingest record passed, so every error file will be reported ' +
                'as unread. Pass --fetched or --fetched-file once you have one.');
  }

  const pageSize = Number((process.env.LIMIT || "dummy-limit") ?? 100);
  const maxPages = Number((process.env.PAGES || "dummy-pages") ?? 20);
  const showAll = process.argv.includes('--show-all');
  const now = Math.floor(Date.now() / 1000);

  let withFile = 0;
  let bad = 0;
  for await (const batch of walk(key, pageSize, maxPages)) {
    const fileId = String(batch.error_file_id ?? '').trim();
    const fileMeta = fileId ? await get(key, `/files/${fileId}`) : null;

    const [state, detail] = verdict(batch, fileMeta, fetched, now);
    const line = `${state.padEnd(15)} ${String(batch.id ?? '?')}  ${detail}`;

    if (fileId) withFile += 1;
    if (FINDINGS.includes(state)) {
      bad += 1;
      console.warn(line);
      console.warn(state === 'aged-out'
        ? '  repair: the content is gone. Re-run the batch from the original ' +
          'input file and diff the output custom_ids against it to find the ' +
          'missing rows.'
        : `  repair: GET /v1/files/${fileId}/content, group the lines by ` +
          'error.code, retry the transient ones (rate_limit_exceeded, ' +
          'server_error) as a new batch, and fix the rest');
      console.warn('  repair: assert error_file_id is null in the ' +
                   'batch-completion handler rather than checking it by hand ' +
                   'once a year');
    } else if (state === 'unresolvable') {
      console.warn(line);
    } else if (showAll || state === 'empty') {
      console.log(line);
    }
  }

  console.log(`${withFile} batch(es) with an error file, ${bad} never fetched`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
