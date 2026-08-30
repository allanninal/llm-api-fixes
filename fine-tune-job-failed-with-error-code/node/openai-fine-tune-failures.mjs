/**
 * Find fine-tuning jobs that were accepted, then failed, and never read.
 *
 * Read only. GET /v1/fine_tuning/jobs, paginated, plus GET on the events feed
 * for jobs that failed. Nothing is created, cancelled or deleted.
 *
 * Creation is asynchronous, so the create call returning 200 says only that the
 * job was accepted. Validation and training failures surface on the job object
 * and nowhere else.
 *
 * The error codes are an open set: documented ones are translated into an
 * action, everything else is printed exactly as returned.
 */
const JOBS_URL = 'https://api.openai.com/v1/fine_tuning/jobs';

const ADVICE = {
  invalid_training_file:
    'the JSONL is malformed. One JSON object per line, no trailing blank line, '
    + 'no BOM, each row a messages array with at least one assistant turn, and '
    + 'one schema across every row.',
  invalid_validation_file:
    'the validation file has the same problem as a malformed training file, and '
    + 'error.param says which of the two was rejected.',
  invalid_n_examples:
    'the example count is out of range: too few rows to train on, or more than '
    + 'the method accepts. Count the lines before uploading.',
  exceeded_quota:
    'this is a billing problem rather than a data one. Editing the file will not '
    + "help; check the account's quota and spend limits.",
};

const FINDINGS = new Set(['job-failed', 'failed-without-error',
  'stalled-in-validation']);

/** One job, reduced. Pure. The error object is flattened. */
export function jobRow(body) {
  const job = (body && typeof body === 'object') ? body : {};
  const error = (job.error && typeof job.error === 'object') ? job.error : {};
  const created = Number(job.created_at ?? 0);
  return {
    id: String(job.id ?? ''),
    status: String(job.status ?? ''),
    model: String(job.model ?? ''),
    fine_tuned_model: String(job.fine_tuned_model ?? ''),
    created_at: Number.isFinite(created) ? Math.trunc(created) : 0,
    code: String(error.code ?? ''),
    param: String(error.param ?? ''),
    message: String(error.message ?? ''),
  };
}

/** Age in hours. Pure. The clock is an argument. */
export function hoursSince(createdAt, now) {
  const created = Number(createdAt);
  const at = Number(now);
  if (!Number.isFinite(created) || !Number.isFinite(at) || created <= 0) return null;
  return (at - created) / 3600;
}

/** The documented meaning of one code. Pure. Empty for anything else. */
export function errorAdvice(code) {
  return ADVICE[String(code ?? '').trim()] ?? '';
}

/** Error-level messages in order. Pure. De-duplicated, never reordered. */
export function errorEvents(events) {
  const out = [];
  for (const item of events ?? []) {
    if (!item || typeof item !== 'object') continue;
    if (String(item.level ?? '').toLowerCase() !== 'error') continue;
    const message = String(item.message ?? '').trim();
    if (message && !out.includes(message)) out.push(message);
  }
  return out;
}

/** Grade one job. Pure. Returns [state, detail]. */
export function classifyJob(row, now, stallHours) {
  const job = row ?? {};
  const status = String(job.status ?? '');
  const id = job.id || '(no id)';
  if (status === 'failed' && job.code) {
    return ['job-failed',
      `${id}: failed on ${job.param || 'an unnamed input'} with ${job.code}`];
  }
  if (status === 'failed') {
    return ['failed-without-error',
      `${id}: failed with no error code on the job object, so the events feed is `
      + 'the only account of why'];
  }
  if (status === 'validating_files') {
    const age = hoursSince(job.created_at, now);
    if (age !== null && age >= stallHours) {
      return ['stalled-in-validation',
        `${id}: ${age.toFixed(1)} hours in validating_files, which is not progress`];
    }
    return ['validating', `${id}: validating files`];
  }
  if (status === 'succeeded') {
    return ['succeeded', `${id}: succeeded, which is a different note`];
  }
  if (status === 'cancelled') {
    return ['cancelled', `${id}: cancelled by somebody on purpose`];
  }
  if (status === 'queued' || status === 'running') {
    return ['running', `${id}: ${status}`];
  }
  return ['unknown-status',
    `${id}: status '${status || '(none)'}' is not one this script recognises`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, code = '') {
  const poll = 'poll the job to a terminal status in CI and fail the build on '
    + 'anything that is not succeeded. A 200 on create is a receipt, not a result.';
  if (state === 'job-failed') {
    const advice = errorAdvice(code);
    if (advice) return [advice, poll];
    return [`the code '${code || '(none)'}' is not one this script has a `
      + 'documented meaning for. Read error.message and the events feed above as '
      + 'printed, and do not act on a guess.', poll];
  }
  if (state === 'failed-without-error') {
    return ['read GET /v1/fine_tuning/jobs/{id}/events for this job. The terminal '
      + 'status is all the job object recorded.', poll];
  }
  if (state === 'stalled-in-validation') {
    return ['read the events feed for the line that validation stopped on, and '
      + 'delete the file if it is a dead upload still counting against project '
      + 'storage.', poll];
  }
  return [];
}

async function fetchJobs(key) {
  const rows = [];
  const params = new URLSearchParams({ limit: '100' });
  for (let page = 0; page < 100; page += 1) {
    let res;
    try {
      res = await fetch(`${JOBS_URL}?${params.toString()}`,
        { headers: { Authorization: `Bearer ${key}` } });
    } catch (err) {
      return [rows, `request failed: ${err.message}`];
    }
    if (res.status !== 200) {
      return [rows, `HTTP ${res.status} ${(await res.text()).slice(0, 160)}`];
    }
    const body = await res.json();
    const data = body.data ?? [];
    for (const item of data) rows.push(jobRow(item));
    if (!body.has_more || !data.length) break;
    params.set('after', data[data.length - 1].id);
  }
  return [rows, null];
}

async function fetchEvents(jobId, key) {
  try {
    const res = await fetch(`${JOBS_URL}/${jobId}/events?limit=100`,
      { headers: { Authorization: `Bearer ${key}` } });
    if (res.status !== 200) return [];
    return [...((await res.json()).data ?? [])].reverse();
  } catch {
    return [];
  }
}

function args(argv) {
  const out = { stallHours: 2 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--stall-hours') out.stallHours = Number(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only. Both '
      + 'calls are GETs of /v1/fine_tuning/jobs');
    process.exitCode = 2;
    return;
  }
  const [rows, err] = await fetchJobs(key);
  if (err) {
    console.error(err);
    process.exitCode = 2;
    return;
  }
  if (!rows.length) {
    console.log('no fine-tuning jobs in this project, so there is nothing to grade');
    return;
  }

  const now = Math.trunc(Date.now() / 1000);
  let findings = 0;
  for (const row of [...rows].sort((a, b) => b.created_at - a.created_at)) {
    const [state, detail] = classifyJob(row, now, opts.stallHours);
    const when = row.created_at
      ? `${new Date(row.created_at * 1000).toISOString().slice(0, 19)}Z` : '(unknown)';
    console.log(`${row.id.padEnd(10)} ${row.status.padEnd(16)} base `
      + `${(row.model || '(none)').padEnd(16)} created ${when}`);
    if (row.code) console.log(`  error.code    ${row.code}`);
    if (row.param) console.log(`  error.param   ${row.param}`);
    if (row.message) console.log(`  error.message ${row.message}`);
    if (FINDINGS.has(state)) {
      const events = errorEvents(await fetchEvents(row.id, key)).slice(0, 5);
      for (const message of events) console.log(`  event         ${message}`);
    }
    console.log(`${state.padEnd(21)} ${detail}`);
    for (const line of repairLines(state, row.code)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }
  console.log(`${rows.length} job(s), ${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
