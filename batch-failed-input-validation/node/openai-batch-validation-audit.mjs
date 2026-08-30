/**
 * Report OpenAI batches that failed input validation, and the lines that broke.
 *
 * Read only. Two GET endpoints, /v1/batches and /v1/files. No upload, no batch
 * creation, no re-submission of a failed batch.
 *
 * Status "failed" on this API means the input file did not survive validation,
 * which happens before any request reaches the model: request_counts is all
 * zeros and nothing was billed. Rows that failed inside a batch that ran are a
 * different status and a different note.
 */
const BATCHES_URL = 'https://api.openai.com/v1/batches';
const FILES_URL = 'https://api.openai.com/v1/files';

const PAGE = 100;

export const NOT_BATCH_INPUT = new Set(['user_data', 'assistants', 'fine-tune', 'vision']);

export const KNOWN_CODES = {
  invalid_json: 'a line is not valid JSON. Validate the file locally before '
    + 'upload: every line must parse on its own.',
  duplicate_custom_id: 'two lines share a custom_id. They must be unique within '
    + 'the file, because results come back unordered and custom_id is the only '
    + 'join key.',
  missing_required_parameter: 'a line is missing a required field. Each row needs '
    + 'custom_id, method, url and body.',
  invalid_url: "a line's url does not match the batch endpoint. The two must "
    + 'agree for every row in the file.',
  model_not_found: 'the body names a model this project cannot reach. Check the '
    + 'id against GET /v1/models with the same key.',
  empty_file: 'the input file has no lines in it. The upload succeeded and the '
    + 'content did not.',
};

const FINDINGS = new Set(['validation-failed', 'orphan-input-files']);

/** Batches whose input file was rejected. Pure. */
export function failedBatches(batches) {
  return (batches ?? []).filter((b) => (b ?? {}).status === 'failed');
}

/** Normalised entries from errors.data[]. Pure. Never throws on a shape. */
export function errorRows(batch) {
  const errors = (batch ?? {}).errors;
  const data = errors && typeof errors === 'object' ? errors.data : null;
  const out = [];
  for (const item of data ?? []) {
    if (!item || typeof item !== 'object') continue;
    const parsed = Number.parseInt(item.line, 10);
    out.push({
      code: String(item.code ?? 'unknown'),
      message: String(item.message ?? ''),
      param: item.param ?? null,
      line: Number.isFinite(parsed) ? parsed : null,
    });
  }
  return out;
}

/** {code: [lines, count, message, param]}. Pure. Sorted by code, then line. */
export function linesByCode(rows) {
  const grouped = new Map();
  for (const row of rows ?? []) {
    if (!grouped.has(row.code)) {
      grouped.set(row.code, { lines: new Set(), count: 0, message: '', param: null });
    }
    const slot = grouped.get(row.code);
    slot.count += 1;
    if (row.line !== null && row.line !== undefined) slot.lines.add(row.line);
    if (!slot.message) slot.message = row.message ?? '';
    if (slot.param === null) slot.param = row.param ?? null;
  }
  const out = {};
  for (const code of [...grouped.keys()].sort()) {
    const slot = grouped.get(code);
    out[code] = [[...slot.lines].sort((a, b) => a - b), slot.count, slot.message,
                 slot.param];
  }
  return out;
}

/** True when the batch dispatched no requests at all. Pure. */
export function nothingBilled(batch) {
  const counts = (batch ?? {}).request_counts ?? {};
  return ['total', 'completed', 'failed']
    .every((k) => (Number(counts[k]) || 0) === 0);
}

/** Every input_file_id the account has handed to a batch. Pure. */
export function batchInputIds(batches) {
  const out = new Set();
  for (const b of batches ?? []) {
    if ((b ?? {}).input_file_id) out.add(String(b.input_file_id));
  }
  return out;
}

/** .jsonl files that can never be batch input and never were. Pure. */
export function mispurposedInputs(files, usedIds) {
  const used = usedIds ?? new Set();
  return (files ?? [])
    .filter((f) => f && typeof f === 'object')
    .filter((f) => String(f.filename ?? '').toLowerCase().endsWith('.jsonl'))
    .filter((f) => NOT_BATCH_INPUT.has(String(f.purpose ?? '')))
    .filter((f) => !used.has(String(f.id)))
    .map((f) => ({ id: String(f.id), filename: String(f.filename ?? ''),
                   purpose: String(f.purpose ?? ''), bytes: Number(f.bytes) || 0 }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** True when the batch was created inside the window. Pure. */
export function withinWindow(batch, now, days) {
  if (!days || days <= 0) return true;
  const created = Number((batch ?? {}).created_at) || 0;
  return created >= now - days * 86400;
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(failed, orphans, days) {
  const f = (failed ?? []).length;
  const o = (orphans ?? []).length;
  const window = days && days > 0 ? `in the last ${days} days` : 'in the account';
  if (f && o) {
    return ['validation-failed',
      `${f} batch(es) failed input validation ${window}, and ${o} .jsonl was `
      + 'uploaded under a purpose /v1/batches will not accept'];
  }
  if (f) {
    return ['validation-failed',
      `${f} batch(es) failed input validation ${window} and nothing polled them `
      + 'to find out'];
  }
  if (o) {
    return ['orphan-input-files',
      `${o} .jsonl file(s) sit under a purpose /v1/batches will not accept, `
      + 'referenced by no batch'];
  }
  return ['validation-clean',
    `no batch ${window} failed validation, and every .jsonl in the file list `
    + 'either carries purpose=batch or was used by a batch'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, codes) {
  if (state === 'validation-clean') {
    return ['nothing to change. Keep the assertion that a submitter only logs '
      + 'success once status has left "validating".'];
  }
  const lines = [];
  for (const code of [...new Set(codes ?? [])].sort()) {
    if (KNOWN_CODES[code]) lines.push(`${code}: ${KNOWN_CODES[code]}`);
  }
  if (state === 'validation-failed') {
    lines.push('fix the input at the reported lines, then re-upload with '
      + 'purpose=batch and create the batch again. Nothing was billed, so '
      + 'nothing needs reconciling.');
    lines.push('make the submitter poll. A 200 from batch creation is a receipt, '
      + 'not a result: the only honest success signal is a batch that has left '
      + '"validating".');
  }
  if (state === 'orphan-input-files') {
    lines.push('re-upload each file with purpose=batch and delete the '
      + 'mis-purposed copy, which counts against project storage until you do.');
    lines.push('assert in the upload helper that the purpose matches the '
      + 'endpoint that will consume the file.');
  }
  return lines;
}

async function getJson(url, key, params) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) {
    target.searchParams.set(k, String(v));
  }
  let res;
  try {
    res = await fetch(target, { headers: { Authorization: `Bearer ${key}` } });
  } catch (err) {
    return [null, `request failed: ${err.message}`];
  }
  if (res.status !== 200) {
    let detail = '';
    try { detail = String((await res.json())?.error?.message ?? ''); } catch { detail = ''; }
    return [null, `HTTP ${res.status} ${detail}`];
  }
  try {
    return [await res.json(), null];
  } catch {
    return [null, 'response was not JSON'];
  }
}

async function pageAll(url, key, params, maxPages) {
  const rows = [];
  let after = null;
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const query = { ...(params ?? {}) };
    if (after) query.after = after;
    const [payload, err] = await getJson(url, key, query);
    if (err) return [rows, err];
    const data = payload.data ?? [];
    rows.push(...data);
    if (!payload.has_more || !data.length) break;
    after = data[data.length - 1]?.id;
    if (!after) break;
  }
  return [rows, null];
}

function args(argv) {
  const out = { sinceDays: 30, maxPages: 20 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--since-days') out.sinceDays = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--max-pages') out.maxPages = Number.parseInt(argv[i += 1], 10);
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

  const now = Math.floor(Date.now() / 1000);
  const [batches, err] = await pageAll(BATCHES_URL, key, { limit: PAGE }, opts.maxPages);
  if (err && !batches.length) {
    console.error(`could not read the batch list: ${err}`);
    process.exitCode = 2;
    return;
  }
  if (err) console.log(`batch list stopped early: ${err}`);

  const scoped = batches.filter((b) => withinWindow(b, now, opts.sinceDays));
  const failed = failedBatches(scoped);
  const seenCodes = [];
  for (const b of failed) {
    const billed = nothingBilled(b)
      ? 'nothing billed (0 requests)'
      : 'request_counts is not all zero, which is unusual for failed';
    const stamp = new Date((Number(b.failed_at || b.created_at) || 0) * 1000)
      .toISOString().replace(/\.\d+Z$/, 'Z');
    console.log(`${String(b.id).padEnd(16)} failed at ${stamp}  ${billed}`);
    const groups = linesByCode(errorRows(b));
    if (!Object.keys(groups).length) {
      console.log('  (the errors object is empty, so the reason is not readable '
        + 'from the API)');
    }
    for (const [code, [lines, , message, param]] of Object.entries(groups)) {
      seenCodes.push(code);
      const shown = lines.slice(0, 6).join(', ');
      const more = lines.length > 6 ? ` and ${lines.length - 6} more` : '';
      const where = lines.length ? `lines ${shown}${more}` : 'no line given';
      const extra = param ? `  param ${param}` : '';
      console.log(`  ${code.padEnd(26)} ${where}${extra}`);
      if (message) console.log(`  ${''.padEnd(26)} ${message.slice(0, 140)}`);
    }
  }

  const [files, ferr] = await pageAll(FILES_URL, key, { limit: 10000 }, opts.maxPages);
  if (ferr) console.log(`file list stopped early: ${ferr}`);
  const orphans = mispurposedInputs(files, batchInputIds(batches));
  for (const row of orphans) {
    console.log(`orphan-input    ${row.id}  ${row.filename}  purpose=${row.purpose}  `
      + `${(row.bytes / 1048576).toFixed(1)} MB`);
  }

  const [state, detail] = verdict(failed, orphans, opts.sinceDays);
  console.log(`${state.padEnd(20)} ${detail}`);
  console.log('  measured: status, errors.data[] and request_counts from the '
    + 'batch list, purpose from the file list');
  console.log('  inferred: that the pipeline never polled, since a failed batch '
    + 'is otherwise indistinguishable from one nobody re-ran on purpose');
  for (const line of repairLines(state, seenCodes)) console.log(`  repair: ${line}`);

  const total = failed.length + orphans.length;
  console.log(`${total} finding(s)`);
  process.exitCode = total ? 1 : 0;
  void FINDINGS;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
