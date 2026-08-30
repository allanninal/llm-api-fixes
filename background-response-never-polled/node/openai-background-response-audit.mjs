/**
 * Drive every background response id you hold to a terminal status.
 *
 * Read only. One GET /v1/responses/{response_id} per id. Nothing is created,
 * cancelled or retried.
 *
 * /v1/responses has no list endpoint, so the ids come from your own job table
 * and the audit is bounded by what you wrote down.
 *
 * A 404 is graded differently on a zero-data-retention project, where a
 * background response is kept for roughly ten minutes so polling can work.
 */
import { readFile } from 'node:fs/promises';

const RESPONSES_URL = 'https://api.openai.com/v1/responses';

export const OPEN_STATES = new Set(['queued', 'in_progress']);
export const TERMINAL_STATES = new Set(['completed', 'incomplete', 'failed', 'cancelled']);

export const ZDR_WINDOW = 600;

export const BUCKET_ORDER = ['stranded', 'failed', 'incomplete', 'gone', 'cancelled',
  'running', 'completed', 'aged-out', 'unreadable'];

export const RETRYABLE = ['server_error', 'rate_limit_exceeded'];

const FINDINGS = new Set(['background-stranded', 'background-failed',
  'background-gone', 'background-no-ids']);

/** [[id, createdHint]] from a file body. Pure. Order kept, ids deduped. */
export function readIds(text) {
  const out = [];
  const seen = new Set();
  for (const raw of String(text ?? '').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const at = line.indexOf(',');
    const ident = (at < 0 ? line : line.slice(0, at)).trim();
    const stamp = at < 0 ? '' : line.slice(at + 1).trim();
    if (!ident || seen.has(ident)) continue;
    seen.add(ident);
    const parsed = Number.parseFloat(stamp);
    out.push([ident, Number.isFinite(parsed) ? Math.trunc(parsed) : null]);
  }
  return out;
}

/** Seconds since creation. Pure. Null when neither source has a time. */
export function ageOf(response, hint, now) {
  const raw = Number((response ?? {}).created_at);
  const created = Number.isFinite(raw) ? Math.trunc(raw)
    : (Number.isFinite(Number(hint)) && hint !== null ? Math.trunc(Number(hint)) : null);
  if (created === null) return null;
  return Math.max(0, Math.trunc(now) - created);
}

/** The failure reason, or ''. Pure. Never returns null to be printed. */
export function reasonFor(response) {
  const r = response ?? {};
  const error = r.error;
  if (error && typeof error === 'object' && error.code) return `error.code ${error.code}`;
  const details = r.incomplete_details;
  if (details && typeof details === 'object' && details.reason) {
    return `incomplete_details.reason ${details.reason}`;
  }
  return '';
}

/** Just the error code, or ''. Pure. */
export function errorCode(response) {
  const error = (response ?? {}).error;
  return error && typeof error === 'object' ? String(error.code ?? '') : '';
}

/** [bucket, detail] for one id. Pure. now, the SLA and ZDR are arguments. */
export function classify(record, now, slaSeconds, zdr = false) {
  const http = (record ?? {}).http;
  const response = (record ?? {}).response ?? {};
  const age = ageOf(response, (record ?? {}).created_hint, now);
  if (http === 404) {
    if (zdr && (age === null || age > ZDR_WINDOW)) {
      return ['aged-out', 'HTTP 404, and on a ZDR project a background response '
        + 'is kept only about ten minutes'];
    }
    return ['gone', 'HTTP 404, no longer retrievable'];
  }
  if (http !== 200) return ['unreadable', `HTTP ${http}`];
  const status = String(response.status ?? '');
  if (OPEN_STATES.has(status)) {
    const shown = age === null ? 'an unknown time' : `${Math.floor(age / 60)} min`;
    if (age !== null && age > slaSeconds) return ['stranded', `${status} for ${shown}`];
    return ['running', `${status} for ${shown}, inside the service level`];
  }
  if (status === 'failed') {
    return ['failed', reasonFor(response) || 'failed with no error object'];
  }
  if (status === 'incomplete') {
    return ['incomplete', reasonFor(response) || 'incomplete with no reason'];
  }
  if (status === 'cancelled') return ['cancelled', 'cancelled'];
  if (status === 'completed') return ['completed', ''];
  return ['unreadable',
    `status ${JSON.stringify(status)} is not one of the six documented values`];
}

/** {bucket: count} in a fixed order. Pure. Empty buckets are omitted. */
export function summarise(rows) {
  const counts = new Map();
  for (const row of rows ?? []) {
    counts.set(row?.bucket, (counts.get(row?.bucket) ?? 0) + 1);
  }
  const out = {};
  for (const b of BUCKET_ORDER) if (counts.has(b)) out[b] = counts.get(b);
  return out;
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(rows, slaSeconds) {
  const list = rows ?? [];
  if (!list.length) {
    return ['background-no-ids',
      'no response ids were supplied. /v1/responses has no list endpoint, so an '
      + 'empty id file means those jobs are already unreachable'];
  }
  const counts = summarise(list);
  const minutes = Math.max(1, Math.trunc(slaSeconds / 60));
  const stranded = counts.stranded ?? 0;
  const failed = counts.failed ?? 0;
  const gone = counts.gone ?? 0;
  let tail = '';
  if (failed || gone) {
    const parts = [];
    if (failed) parts.push(`${failed} failed`);
    if (gone) parts.push(`${gone} is no longer retrievable`);
    tail = `, ${parts.join(' and ')}`;
  }
  if (stranded) {
    return ['background-stranded',
      `${stranded} of ${list.length} ids have been queued or in_progress past `
      + `the ${minutes} minute service level${tail}`];
  }
  if (failed) {
    return ['background-failed',
      `${failed} of ${list.length} ids reached failed and nothing read the error `
      + `code${gone ? `, ${gone} is no longer retrievable` : ''}`];
  }
  if (gone) {
    return ['background-gone',
      `${gone} of ${list.length} ids no longer resolve, so whatever they `
      + 'produced is gone'];
  }
  return ['background-drained',
    `all ${list.length} ids are terminal or inside the ${minutes} minute service level`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, rows) {
  const list = rows ?? [];
  if (state === 'background-no-ids') {
    return ['persist the response id transactionally with the job row, not after '
      + 'the call returns. A crash in between leaves a job that runs, bills, and '
      + 'is referenced nowhere.',
    'there is no list endpoint for /v1/responses, so an id you did not write '
      + 'down cannot be recovered by any read call.'];
  }
  if (state === 'background-drained') {
    return ['nothing stranded. Keep the reconciler running: the failure mode '
      + 'here is a poller that stops, not one that is wrong.'];
  }
  const lines = [];
  const codes = new Set(list.map((r) => r?.code).filter(Boolean));
  const retry = [...codes].filter((c) => RETRYABLE.includes(c)).sort();
  const escalate = [...codes].filter((c) => !RETRYABLE.includes(c)).sort();
  if (retry.length) {
    lines.push(`retry the transient codes (${retry.join(', ')}), which will `
      + 'usually succeed on a second attempt.');
  }
  if (escalate.length) {
    lines.push(`escalate ${escalate.join(', ')}. These fail identically on every `
      + 'attempt, so a retry loop only spends money.');
  }
  if (list.some((r) => r?.bucket === 'stranded')) {
    lines.push('cancel the stranded jobs you no longer want, at '
      + '/v1/responses/{response_id}/cancel. Only responses created with '
      + 'background true can be cancelled, so these ones can be.');
  }
  if (list.some((r) => r?.bucket === 'incomplete')) {
    lines.push('an incomplete response was cut rather than refused. Read '
      + 'incomplete_details.reason: max_output_tokens wants a bigger cap, '
      + 'content_filter wants a person.');
  }
  if (list.some((r) => r?.bucket === 'gone')) {
    lines.push('an id that no longer resolves cannot be recovered by any read '
      + 'call. Archive the output at the moment a response reaches completed, '
      + 'not on the next run of a nightly job.');
  }
  return lines;
}

async function fetchOne(id, key) {
  let res;
  try {
    res = await fetch(`${RESPONSES_URL}/${id}`, {
      headers: { Authorization: `Bearer ${key}` },
    });
  } catch {
    return [null, {}];
  }
  try {
    return [res.status, await res.json()];
  } catch {
    return [res.status, {}];
  }
}

function args(argv) {
  const out = { slaMinutes: 30, zdr: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--ids') out.ids = argv[i += 1];
    else if (argv[i] === '--sla-minutes') out.slaMinutes = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--zdr') out.zdr = true;
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only');
    process.exitCode = 2;
    return;
  }

  let raw = '';
  if (opts.ids) {
    try {
      raw = await readFile(opts.ids, 'utf8');
    } catch (err) {
      console.error(`could not read ${opts.ids}: ${err.message}`);
      process.exitCode = 2;
      return;
    }
  } else {
    raw = ((process.env.OPENAI_RESPONSE_IDS || "dummy-openai-response-ids") ?? '').split(',').join('\n');
  }

  const pairs = readIds(raw);
  const now = Math.floor(Date.now() / 1000);
  const sla = Math.max(1, opts.slaMinutes) * 60;
  const rows = [];
  for (const [ident, hint] of pairs) {
    const [http, payload] = await fetchOne(ident, key);
    const [bucket, detail] = classify({ http, response: payload, created_hint: hint },
      now, sla, opts.zdr);
    rows.push({ id: ident, bucket, detail, code: errorCode(payload) });
    console.log(`${ident.slice(0, 16).padEnd(16)} ${bucket.padEnd(12)} ${detail}`);
  }

  const [state, detail] = verdict(rows, sla);
  console.log(`${state.padEnd(20)} ${detail}`);
  const counts = summarise(rows);
  if (Object.keys(counts).length) {
    console.log(`  buckets: ${Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(', ')}`);
  }
  console.log('  measured: status, error.code and incomplete_details.reason from '
    + 'one GET per id');
  console.log('  inferred: nothing about ids not in the file, because '
    + '/v1/responses has no list endpoint and cannot be enumerated');
  for (const line of repairLines(state, rows)) console.log(`  repair: ${line}`);

  let findings = ['stranded', 'failed', 'gone'].reduce((n, b) => n + (counts[b] ?? 0), 0);
  if (state === 'background-no-ids') findings = 1;
  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
  void FINDINGS;
  void TERMINAL_STATES;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
