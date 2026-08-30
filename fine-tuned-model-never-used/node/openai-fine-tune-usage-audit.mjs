/**
 * Report OpenAI fine-tuned models that were trained, billed, and never called.
 *
 * Read only. GET requests and nothing else, and it needs two credentials
 * because no single key can answer the question:
 *
 *   OPENAI_API_KEY    a project key set to Read Only, for /v1/fine_tuning/jobs,
 *                     /v1/models and /v1/files
 *   OPENAI_ADMIN_KEY  an organization admin key with read scopes, for
 *                     /v1/organization/usage/completions
 *
 * The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// Published platform dates. Fine-tuned snapshots on a retired base model stop
// answering on the first; new fine-tuning jobs cannot be created after the second.
const BASE_RETIREMENT = '2026-10-23';
const NEW_JOBS_BLOCKED = '2027-01-06';

const FINDINGS = ['never-called', 'never-called-base-gone', 'in-service-base-gone'];

/**
 * The base model a fine-tune id was built on, or null. Pure.
 * "ft:gpt-4o-mini-2024-07-18:acme::AbC123" -> "gpt-4o-mini-2024-07-18".
 */
export function baseModel(fineTunedModel) {
  const name = String(fineTunedModel ?? '').trim();
  if (!name.toLowerCase().startsWith('ft:')) return null;
  const parts = name.split(':');
  if (parts.length < 3 || !parts[1]) return null;
  return parts[1];
}

/**
 * Whole days from now until an ISO date, or null if unreadable. Pure.
 * Negative once the date has passed, and floored toward the past so a deadline
 * fourteen hours away reads as 0 days rather than 1.
 */
export function daysUntil(dateStr, now) {
  const parts = String(dateStr ?? '').split('-');
  if (parts.length !== 3) return null;
  const [year, month, day] = parts.map((p) => Number(p));
  if (![year, month, day].every(Number.isFinite)) return null;
  const target = Date.UTC(year, month - 1, day);
  const from = now instanceof Date ? now.getTime() : Number(now);
  if (!Number.isFinite(from)) return null;
  return Math.floor((target - from) / 86400000);
}

/**
 * Classify one fine-tuning job against its usage. Pure. Returns [state, detail].
 * A base model missing from GET /v1/models puts a deadline on the custom model
 * whether or not anyone is using it, so that case is split out.
 */
export function verdict(job, requestsMade, availableModels, now, windowDays = 30) {
  const status = String(job.status ?? '').trim().toLowerCase();
  if (status !== 'succeeded') {
    return ['not-succeeded',
      `status is ${status || 'missing'}, so there is no model id to look for ` +
      'usage against'];
  }

  const modelId = String(job.fine_tuned_model ?? '').trim();
  if (!modelId) {
    return ['unnamed',
      'the job succeeded and carries no fine_tuned_model. Read the object by ' +
      'hand rather than assuming nothing was produced.'];
  }

  const trainedRaw = Number(job.trained_tokens ?? 0);
  const trained = Number.isFinite(trainedRaw) ? Math.trunc(trainedRaw) : 0;
  const callsRaw = Number(requestsMade ?? 0);
  const calls = Number.isFinite(callsRaw) ? Math.trunc(callsRaw) : 0;

  const base = job.model ?? baseModel(modelId);
  const available = new Set(availableModels ?? []);
  const baseGone = Boolean(base) && !available.has(base);
  const deadline = daysUntil(BASE_RETIREMENT, now);
  const clock = deadline === null ? ''
    : ` Fine-tunes on retired base models stop answering in ${deadline} day(s).`;

  if (calls > 0) {
    if (baseGone) {
      return ['in-service-base-gone',
        `${calls} request(s) in ${windowDays} days, but the base model ${base} ` +
        'is no longer listed by GET /v1/models. This fine-tune is serving ' +
        `traffic and is going to stop.${clock}`];
    }
    return ['in-service', `${calls} request(s) in ${windowDays} days`];
  }

  if (baseGone) {
    return ['never-called-base-gone',
      `0 request(s) in ${windowDays} days, ${trained} trained token(s), and ` +
      `the base model ${base} is no longer listed. Nothing to migrate and ` +
      `nothing to lose.${clock}`];
  }

  return ['never-called',
    `0 request(s) in ${windowDays} days, ${trained} trained token(s). Training ` +
    'was billed and inference never happened.'];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error(`401 from OpenAI on ${path}: wrong key for this endpoint. ` +
      'Jobs, models and files want the project key; usage wants the admin key.');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* walkJobs(key, maxPages = 20) {
  let params = { limit: 100 };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/fine_tuning/jobs', params);
    const data = page.data ?? [];
    for (const job of data) yield job;
    if (!page.has_more || data.length === 0) return;
    params = { limit: 100, after: data[data.length - 1].id };
  }
}

async function requestsByModel(key, startTime, days, maxPages = 20) {
  const out = {};
  let params = {
    start_time: startTime, bucket_width: '1d', limit: days, group_by: 'model',
  };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/usage/completions', params);
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const model = String(result.model ?? '');
        if (!model) continue;
        const n = Number(result.num_model_requests ?? 0);
        if (Number.isFinite(n)) out[model] = (out[model] ?? 0) + Math.trunc(n);
      }
    }
    if (!page.next_page) return out;
    params = { ...params, page: page.next_page };
  }
  return out;
}

async function availableModelIds(key) {
  const page = await get(key, '/models');
  return new Set((page.data ?? []).filter((m) => m.id).map((m) => String(m.id)));
}

async function resultFileBytes(key) {
  const page = await get(key, '/files', { purpose: 'fine-tune-results', limit: 100 });
  const files = page.data ?? [];
  let total = 0;
  for (const f of files) {
    const n = Number(f.bytes ?? 0);
    if (Number.isFinite(n)) total += Math.trunc(n);
  }
  return [files.length, total];
}

async function main() {
  const projectKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const adminKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!projectKey || !adminKey) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only) and ' +
                  'OPENAI_ADMIN_KEY (an organization admin key with read scopes)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const showAll = process.argv.includes('--show-all');

  const nowMs = Date.now();
  const now = new Date(nowMs);
  const start = Math.floor(nowMs / 1000) - days * 86400;

  const usage = await requestsByModel(adminKey, start, days);
  const available = await availableModelIds(projectKey);

  let checked = 0;
  let bad = 0;
  for await (const job of walkJobs(projectKey)) {
    const modelId = String(job.fine_tuned_model ?? '');
    const [state, detail] = verdict(job, usage[modelId] ?? 0, available, now, days);
    if (state !== 'not-succeeded') checked += 1;
    const line = `${state.padEnd(22)} ${(modelId || job.id).padEnd(42)} ${detail}`;

    if (FINDINGS.includes(state)) {
      bad += 1;
      console.warn(line);
      const page = await get(projectKey, `/fine_tuning/jobs/${job.id}/checkpoints`);
      for (const cp of page.data ?? []) {
        const cpId = cp.fine_tuned_model_checkpoint;
        if (cpId) {
          console.warn(`  checkpoint ${cpId}: ${usage[String(cpId)] ?? 0} ` +
                       'request(s) in the window');
        }
      }
      console.warn('  repair: route traffic to it or retire it. Deleting the ' +
        'custom model and its result_files stops the storage charge; ' +
        'GET /v1/files?purpose=fine-tune-results lists them.');
      const left = daysUntil(NEW_JOBS_BLOCKED, now);
      if (left !== null) {
        console.warn('  repair: decide before the platform decides. New ' +
          `fine-tuning jobs cannot be created after ${NEW_JOBS_BLOCKED}, ` +
          `${left} day(s) away.`);
      }
    } else if (showAll) {
      console.log(line);
    }
  }

  const [count, totalBytes] = await resultFileBytes(projectKey);
  if (count) {
    console.log(`${count} fine-tune result file(s) still stored, ` +
                `${(totalBytes / 1048576).toFixed(1)} MB`);
  }

  console.log(`${checked} succeeded job(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this from the test file does not
// run main(), fail on the missing keys, and set an exit code that fails the suite.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
