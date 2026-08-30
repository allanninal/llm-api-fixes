/**
 * Find batch results that were paid for and never collected, on both providers.
 *
 * Read only: /v1/batches and /v1/files on OpenAI, /v1/messages/batches on
 * Anthropic. Nothing is downloaded, deleted or re-run.
 *
 * Neither API records whether you read a result, so the ledger of what your
 * consumer processed is an input and an empty one is a verdict.
 *
 * An OpenAI batch output file is deleted 30 days after the batch is complete;
 * the file object's own expires_at wins where it is set. Claude batch results
 * are available 29 days after the batch was created, not after it ended.
 */
import { readFile } from 'node:fs/promises';

const OPENAI_BATCHES_URL = 'https://api.openai.com/v1/batches';
const OPENAI_FILES_URL = 'https://api.openai.com/v1/files';
const ANTHROPIC_BATCHES_URL = 'https://api.anthropic.com/v1/messages/batches';

export const OPENAI_RETENTION = 30 * 86400;
export const ANTHROPIC_RETENTION = 29 * 86400;
export const OPEN_WINDOW = 24 * 3600;
export const GRACE = 2 * 3600;

const OPENAI_TERMINAL = new Set(['completed', 'failed', 'expired', 'cancelled']);

const FINDINGS = new Set(['batch-output-expiring', 'batch-output-lost',
  'batch-output-unclaimed', 'batch-never-polled']);

/** Set of batch ids your consumer has processed. Pure. */
export function readLedger(text) {
  const out = new Set();
  for (const raw of String(text ?? '').split(',').join('\n').split('\n')) {
    const line = raw.trim();
    if (line && !line.startsWith('#')) out.add(line);
  }
  return out;
}

/** Epoch seconds from a unix number or an RFC 3339 string. Pure. */
export function parseTime(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
    return null;
  }
  if (typeof value === 'number') return Math.trunc(value);
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

/** {file_id: file object}. Pure. */
export function fileIndex(files) {
  const out = {};
  for (const f of files ?? []) {
    if (f && typeof f === 'object' && f.id) out[String(f.id)] = f;
  }
  return out;
}

/** [epoch, source] for when this output disappears. Pure. */
export function openaiDeadline(batch, fileObj) {
  const stamp = parseTime((fileObj ?? {}).expires_at);
  if (stamp) return [stamp, 'expires_at'];
  const completed = parseTime((batch ?? {}).completed_at);
  if (completed) return [completed + OPENAI_RETENTION, 'completed_at + 30d'];
  const created = parseTime((batch ?? {}).created_at);
  if (created) return [created + OPENAI_RETENTION, 'created_at + 30d'];
  return [null, 'unknown'];
}

/** Whole days until the deadline. Pure. Null when there is no deadline. */
export function daysLeft(deadline, now) {
  if (deadline === null || deadline === undefined) return null;
  return Math.floor((deadline - now) / 86400);
}

/** One row per OpenAI batch worth reporting. Pure. */
export function openaiRows(batches, index, ledger, now, warnDays) {
  const rows = [];
  for (const b of batches ?? []) {
    const status = String((b ?? {}).status ?? '');
    const id = String(b.id);
    const created = parseTime(b.created_at);
    if (!OPENAI_TERMINAL.has(status)) {
      if (created !== null && now - created > OPEN_WINDOW + GRACE) {
        rows.push({ provider: 'openai', id, state: 'stalled', done: 0, days: null,
          detail: `${status} for ${Math.floor((now - created) / 3600)} h, past the 24 h window` });
      }
      continue;
    }
    if (status !== 'completed') continue;
    const done = Number((b.request_counts ?? {}).completed) || 0;
    const artifact = b.output_file_id;
    if (!artifact) continue;
    if (!(String(artifact) in (index ?? {}))) {
      rows.push({ provider: 'openai', id, state: 'lost', done, days: null,
        detail: `output_file_id ${artifact} no longer exists` });
      continue;
    }
    const [deadline, source] = openaiDeadline(b, index[String(artifact)]);
    const left = daysLeft(deadline, now);
    let state;
    let detail;
    if (left !== null && left <= warnDays) {
      state = 'expiring';
      detail = `${done} completed, ${Math.max(0, left)} days left (${source})`;
    } else if ((ledger ?? new Set()).has(id)) {
      state = 'claimed';
      detail = `${done} completed, in the ingest ledger`;
    } else {
      state = 'unclaimed';
      detail = `${done} completed, ${left === null ? 'unknown' : Math.max(0, left)} days left`;
    }
    rows.push({ provider: 'openai', id, state, done, days: left, detail });
  }
  return rows;
}

/** One row per Claude batch worth reporting. Pure. */
export function anthropicRows(batches, ledger, now, warnDays) {
  const rows = [];
  for (const b of batches ?? []) {
    const id = String((b ?? {}).id);
    const status = String(b.processing_status ?? '');
    const created = parseTime(b.created_at);
    const done = Number((b.request_counts ?? {}).succeeded) || 0;
    if (status !== 'ended') {
      if (created !== null && now - created > OPEN_WINDOW + GRACE) {
        rows.push({ provider: 'anthropic', id, state: 'stalled', done, days: null,
          detail: `${status || 'unknown'} for ${Math.floor((now - created) / 3600)} h, `
            + 'past the 24 h window' });
      }
      continue;
    }
    if (done <= 0) continue;
    if (b.archived_at) {
      rows.push({ provider: 'anthropic', id, state: 'lost', done, days: null,
        detail: `archived_at set, ${done} succeeded, gone` });
      continue;
    }
    const left = created === null ? null : daysLeft(created + ANTHROPIC_RETENTION, now);
    let state;
    let detail;
    if (left !== null && left <= warnDays) {
      state = 'expiring';
      detail = `${done} succeeded, ${Math.max(0, left)} days left (created_at + 29d)`;
    } else if ((ledger ?? new Set()).has(id)) {
      state = 'claimed';
      detail = `${done} succeeded, in the ingest ledger`;
    } else {
      state = 'unclaimed';
      detail = `${done} succeeded, ${left === null ? 'unknown' : Math.max(0, left)} days left`;
    }
    rows.push({ provider: 'anthropic', id, state, done, days: left, detail });
  }
  return rows;
}

/** Rows ordered by what you can still act on. Pure. */
export function byUrgency(rows) {
  const rank = { expiring: 0, lost: 1, unclaimed: 2, stalled: 3, claimed: 4 };
  return [...(rows ?? [])].sort((a, b) => {
    const ra = rank[a.state] ?? 9;
    const rb = rank[b.state] ?? 9;
    if (ra !== rb) return ra - rb;
    const da = a.days === null || a.days === undefined ? 99999 : a.days;
    const db = b.days === null || b.days === undefined ? 99999 : b.days;
    if (da !== db) return da - db;
    return String(a.id ?? '').localeCompare(String(b.id ?? ''));
  });
}

/** {state: n}. Pure. */
export function countsByState(rows) {
  const out = {};
  for (const row of rows ?? []) out[row.state] = (out[row.state] ?? 0) + 1;
  return out;
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(rows, ledger, warnDays) {
  const list = rows ?? [];
  if (!list.length) {
    return ['batch-output-clean',
      'every batch on the providers checked is either open inside its window or '
      + 'terminal with its output accounted for'];
  }
  const c = countsByState(list);
  const parts = [];
  if (c.lost) parts.push(`${c.lost} are already unrecoverable`);
  if (c.unclaimed) parts.push(`${c.unclaimed} were never claimed`);
  if (c.stalled) parts.push(`${c.stalled} never reached a terminal state`);
  const tail = parts.length ? `, ${parts.join(', ')}` : '';
  if (c.expiring) {
    return ['batch-output-expiring',
      `${c.expiring} batch(es) hold results that expire within ${warnDays} days${tail}`];
  }
  if (c.lost) {
    const rest = parts.slice(1);
    return ['batch-output-lost',
      `${c.lost} batch(es) hold results that are already gone and can only be `
      + `recovered by re-running them${rest.length ? `, ${rest.join(', ')}` : ''}`];
  }
  if (c.unclaimed) {
    let detail = `${c.unclaimed} batch(es) ended with results nothing has collected`;
    if (!ledger || !ledger.size) {
      detail += ', and no ingest ledger was supplied, so every terminal batch '
        + 'counts as unclaimed';
    }
    return ['batch-output-unclaimed', detail];
  }
  if (c.stalled) {
    return ['batch-never-polled',
      `${c.stalled} batch(es) have been open longer than the 24 hour window, `
      + 'which means nothing has polled them'];
  }
  return ['batch-output-clean',
    `all ${list.length} terminal batch(es) are in the ingest ledger with runway `
    + 'left on the clock'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, rows, ledger) {
  const c = countsByState(rows ?? []);
  if (state === 'batch-output-clean') {
    return ['nothing outstanding. Keep the assertion that a batch is not done '
      + 'until its output has been archived into your own store.'];
  }
  const lines = [];
  if (c.expiring) {
    lines.push('download the expiring outputs today and persist them keyed by '
      + 'batch id. After the clock runs out no read call recovers them.');
  }
  if (c.lost) {
    lines.push('the lost ones must be re-run and re-paid. Nothing in either API '
      + 'can return results after the retention window closes.');
  }
  if (c.unclaimed) {
    lines.push('sweep the unclaimed batches: list, diff against your ledger, '
      + 'download, and key the rows by custom_id, which is the only join '
      + 'available since results are not returned in request order.');
  }
  if (c.stalled) {
    lines.push('a batch open past 24 hours is a stale object rather than a slow '
      + 'job. Poll every id you create to a terminal state, and record the id at '
      + 'creation time so orphans are identifiable.');
  }
  if (!ledger || !ledger.size) {
    lines.push('no ingest ledger was supplied, so nothing could be confirmed as '
      + 'consumed. Record every batch id your consumer processes: neither API '
      + 'offers a read receipt.');
  }
  lines.push('run the error-file audit alongside this one. That note reads '
    + 'error_file_id, the list of rows that failed; this one reads the work '
    + 'itself. Both assertions belong in the same batch completion handler.');
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

async function page(url, headers, params, maxPages, cursor = 'after') {
  const rows = [];
  let token = null;
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const query = { ...(params ?? {}) };
    if (token) query[cursor] = token;
    const [payload, err] = await getJson(url, headers, query);
    if (err) return [rows, err];
    const data = payload.data ?? [];
    rows.push(...data);
    if (!payload.has_more || !data.length) break;
    token = payload.last_id ?? data[data.length - 1]?.id;
    if (!token) break;
  }
  return [rows, null];
}

function args(argv) {
  const out = { warnDays: 5, maxPages: 20 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--ledger') out.ledger = argv[i += 1];
    else if (argv[i] === '--warn-days') out.warnDays = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--max-pages') out.maxPages = Number.parseInt(argv[i += 1], 10);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const openaiKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const anthropicKey = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!openaiKey && !anthropicKey) {
    console.error('set OPENAI_API_KEY (project key, Read Only) or '
      + 'ANTHROPIC_API_KEY (workspace key), or both');
    process.exitCode = 2;
    return;
  }

  let raw = (process.env.BATCH_INGEST_LEDGER || "dummy-batch-ingest-ledger") ?? '';
  if (opts.ledger) {
    try {
      raw = await readFile(opts.ledger, 'utf8');
    } catch (err) {
      console.error(`could not read ${opts.ledger}: ${err.message}`);
      process.exitCode = 2;
      return;
    }
  }
  const ledger = readLedger(raw);

  const now = Math.floor(Date.now() / 1000);
  const rows = [];
  const checked = [];
  if (openaiKey) {
    checked.push('openai');
    const headers = { Authorization: `Bearer ${openaiKey}` };
    const [batches, err] = await page(OPENAI_BATCHES_URL, headers, { limit: 100 },
      opts.maxPages);
    if (err) console.log(`openai batch list stopped early: ${err}`);
    const [files, ferr] = await page(OPENAI_FILES_URL, headers,
      { limit: 10000, purpose: 'batch_output' }, opts.maxPages);
    if (ferr) console.log(`openai file list stopped early: ${ferr}`);
    rows.push(...openaiRows(batches, fileIndex(files), ledger, now, opts.warnDays));
  }
  if (anthropicKey) {
    checked.push('anthropic');
    const headers = { 'x-api-key': anthropicKey, 'anthropic-version': '2023-06-01' };
    const [batches, err] = await page(ANTHROPIC_BATCHES_URL, headers, { limit: 1000 },
      opts.maxPages, 'after_id');
    if (err) console.log(`anthropic batch list stopped early: ${err}`);
    rows.push(...anthropicRows(batches, ledger, now, opts.warnDays));
  }

  const reportable = rows.filter((r) => r.state !== 'claimed');
  for (const row of byUrgency(reportable)) {
    console.log(`${row.provider.padEnd(10)} ${row.id.slice(0, 14).padEnd(14)} `
      + `${row.state.padEnd(12)} ${row.detail}`);
  }

  const [state, detail] = verdict(reportable, ledger, opts.warnDays);
  console.log(`${state.padEnd(22)} ${detail}`);
  console.log(`  checked: ${checked.join(', ') || 'nothing'}, ${ledger.size} `
    + 'batch id(s) in the ledger');
  console.log('  measured: status, the result artifact and the retention clock '
    + 'from the batch lists, and file existence from the file list');
  console.log('  inferred: that an id absent from your ledger was never consumed, '
    + 'since neither API records whether a result was downloaded');
  for (const line of repairLines(state, reportable, ledger)) {
    console.log(`  repair: ${line}`);
  }

  console.log(`${reportable.length} finding(s)`);
  process.exitCode = reportable.length ? 1 : 0;
  void FINDINGS;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
