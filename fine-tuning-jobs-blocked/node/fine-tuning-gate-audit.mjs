/**
 * Grade two verbs on one resource: creating a fine-tuning job, and serving one.
 *
 * Read only. Every request is a GET: the job listing, the usage report, and
 * the model objects for each fine-tune and its base. Nothing here submits a
 * job. The obvious way to find out whether creation is still accepted is to
 * attempt one, and attempting one spends money, trains a model nobody asked
 * for, and is a write.
 *
 * So eligibility is computed from readable state: the date, whether the job
 * list is non-empty, and how long since any ft: model produced a request.
 */
export const API = 'https://api.openai.com/v1';

// Announced 7 May 2026, in three stages. The middle one is a rolling window
// over your own traffic rather than a date.
export const NEVER_FINE_TUNED = '2026-05-07';
export const NO_RECENT_INFERENCE = '2026-07-02';
export const CUTOFF = '2027-01-06';
export const WINDOW = 60;

// Inference on a fine-tune dies with its base. Fallback only, and labelled.
export const BASE_SHUTDOWN = '2026-10-23';

// Exact or hyphen-delimited, never a loose prefix: gpt-4.1-nano starts with
// the characters gpt-4 and must not be filed under ft-gpt-4.
export const FAMILIES = [
  ['gpt-3.5-turbo', 'ft-gpt-3.5-turbo', 'gpt-5.6-terra'],
  ['gpt-4.1-nano-2025-04-14', 'ft-gpt-4.1-nano-2025-04-14', 'gpt-5.6-luna'],
  ['gpt-4', 'ft-gpt-4', 'gpt-5.6-sol'],
  ['babbage-002', 'ft-babbage-002', 'gpt-5.6-terra'],
  ['davinci-002', 'ft-davinci-002', 'gpt-5.6-terra'],
  ['o4-mini-2025-04-16', 'ft-o4-mini-2025-04-16', 'gpt-5.6-terra'],
];

const FINDINGS = new Set(['blocked-never-fine-tuned', 'blocked-no-recent-inference',
  'eligibility-expiring', 'create-closed', 'unknown-eligibility', 'already-dead',
  'dying-soon', 'no-base-date']);

const REPAIRS = {
  'blocked-never-fine-tuned':
    `this organization has no fine-tuning history and the ${NEVER_FINE_TUNED} `
    + 'restriction has passed, so creation is already refused. Nothing reopens that.',
  'blocked-no-recent-inference':
    'route real traffic to a fine-tune to reopen the window, or accept that this '
    + `organization is out of the fine-tuning business as of ${NO_RECENT_INFERENCE}.`,
  'eligibility-expiring':
    'the 60 day window is closing. Either retrain now, while creating a job is '
    + 'still permitted, or keep a real workload on a fine-tuned model so the clock '
    + 'does not run out on a quiet week.',
  'create-closed':
    `the ${CUTOFF} cutoff has passed and no organization can create a fine-tuning `
    + 'job. Whatever is still serving is the last of it.',
  'unknown-eligibility':
    'the inference clock could not be read, so eligibility is unknown rather than '
    + 'fine. Re-run with an admin-read key before planning around it.',
  'already-dead':
    'the base is past its shutdown date, so this fine-tune has stopped serving. '
    + 'Retraining onto a supported base is the only route back, and it is only '
    + `available until ${CUTOFF}.`,
  'dying-soon':
    'retrain onto the supported base before the date. Where the fine-tune only ever '
    + 'encoded formatting, evaluate replacing it with prompting plus structured '
    + 'outputs instead of retraining at all.',
  'no-base-date':
    'neither the model object nor the published table has a date for this base, so '
    + 'its serving deadline is unknown. Treat it as undated rather than as safe.',
};

const day = (iso) => Date.parse(`${iso}T00:00:00Z`);

/** Whole days from today to a date. Pure. Negative once it has passed. */
export function daysLeft(today, when) {
  return Math.round((day(String(when)) - day(String(today))) / 86400000);
}

/** Can this organization still create a job? Pure. [state, detail]. */
export function createEligibility(today, hasPriorJobs, daysSinceFtInference) {
  if (daysLeft(today, CUTOFF) < 0) {
    return ['create-closed',
      `the ${CUTOFF} cutoff has passed, so no organization can create a fine-tuning job`];
  }
  if (!hasPriorJobs && daysLeft(today, NEVER_FINE_TUNED) < 0) {
    return ['blocked-never-fine-tuned',
      `the job list is empty and the ${NEVER_FINE_TUNED} restriction has passed, so `
      + 'this organization cannot create a job today. Read from the listing, not '
      + 'from an attempt'];
  }
  if (daysLeft(today, NO_RECENT_INFERENCE) >= 0) {
    return ['eligible',
      `the 60 day inference rule does not apply until ${NO_RECENT_INFERENCE}; `
      + `${daysLeft(today, CUTOFF)} day(s) until the ${CUTOFF} cutoff`];
  }
  if (daysSinceFtInference === null || daysSinceFtInference === undefined) {
    return ['unknown-eligibility',
      'the inference clock could not be read, so eligibility is unknown rather than fine'];
  }
  if (daysSinceFtInference === 'none-in-window') {
    return ['blocked-no-recent-inference',
      'no fine-tuned model produced a request anywhere in the window read, so the '
      + `${WINDOW} day rule has already closed creation. Read from usage, not from `
      + 'an attempt'];
  }
  const days = Number(daysSinceFtInference);
  if (days > WINDOW) {
    return ['blocked-no-recent-inference',
      `no fine-tuned model has served a request for ${days} day(s), and the ${WINDOW} `
      + `day rule has applied since ${NO_RECENT_INFERENCE}, so new jobs are already `
      + 'being refused. Read from usage, not from an attempt'];
  }
  if (days > 45) {
    return ['eligibility-expiring',
      `the last fine-tuned request was ${days} day(s) ago, so ${WINDOW - days} day(s) `
      + `of the ${WINDOW} day window are left`];
  }
  return ['eligible',
    `the last fine-tuned request was ${days} day(s) ago and ${daysLeft(today, CUTOFF)} `
    + `day(s) remain until the ${CUTOFF} cutoff`];
}

/** The deprecation family and replacement for a base. Pure. [family, to]. */
export function familyFor(baseModel) {
  const base = String(baseModel ?? '');
  for (const [prefix, family, replacement] of FAMILIES) {
    if (base === prefix || base.startsWith(`${prefix}-`)) return [family, replacement];
  }
  return [null, null];
}

/** When this fine-tune stops serving. Pure. [date, source, detail]. */
export function servingDeadline(apiShutdownDate, family) {
  if (apiShutdownDate) {
    return [String(apiShutdownDate), 'api', 'shutdown_date read off the model object'];
  }
  if (family) {
    return [BASE_SHUTDOWN, 'published-table',
      `the model object carried no shutdown_date, so this is the ${family} row in `
      + 'the deprecation table'];
  }
  return [null, 'unknown',
    'neither the model object nor the published table has a date for this base'];
}

/** Grade one job's serving half. Pure. [state, detail]. */
export function jobVerdict(status, fineTunedModel, deadline, today) {
  if (String(status) !== 'succeeded' || !fineTunedModel) {
    return ['not-serving',
      `status ${status} with no fine-tuned model, so nothing is serving from this job`];
  }
  if (!deadline) {
    return ['no-base-date', 'no serving deadline could be established for this base'];
  }
  const left = daysLeft(today, deadline);
  if (left < 0) {
    return ['already-dead',
      `the base shut down ${-left} day(s) ago, so this fine-tune has stopped serving`];
  }
  if (left <= 90) return ['dying-soon', `${left} day(s) of inference left`];
  return ['serving', `${left} day(s) of inference left`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, replacement = null) {
  const line = REPAIRS[state];
  if (!line) return [];
  if ((state === 'dying-soon' || state === 'already-dead') && replacement) {
    return [line, `the documented replacement base is ${replacement}.`];
  }
  if (state === 'blocked-no-recent-inference') {
    return [line,
      `note the order the dates fall in: the bases die ${BASE_SHUTDOWN} and the right `
      + `to retrain closes ${CUTOFF}, so October is the deadline and January is only `
      + 'the outside edge.'];
  }
  return [line];
}

async function getJson(path, key, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(one));
  }
  try {
    const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
    let body = {};
    try { body = await r.json(); } catch { body = {}; }
    return [r.status, body];
  } catch {
    return [null, {}];
  }
}

async function allJobs(key, pages = 20) {
  const out = [];
  let after = null;
  for (let i = 0; i < pages; i += 1) {
    const params = { limit: 100 };
    if (after) params.after = after;
    const [status, body] = await getJson('/fine_tuning/jobs', key, params);
    if (status !== 200) {
      console.log(`job listing came back ${status}, so eligibility cannot be read from it`);
      break;
    }
    const page = body.data || [];
    out.push(...page);
    if (!page.length || !body.has_more) break;
    after = page[page.length - 1].id;
    if (!after) break;
  }
  return out;
}

async function daysSinceFtInference(key, today, days = 70) {
  const start = Math.floor(Date.now() / 1000) - days * 86400;
  const [status, body] = await getJson('/organization/usage/completions', key, {
    start_time: start, bucket_width: '1d', 'group_by[]': ['model'], limit: 180,
  });
  if (status !== 200) {
    console.log(`usage report came back ${status}, so the inference clock could not be read`);
    return null;
  }
  let last = null;
  for (const bucket of body.data || []) {
    if (!bucket.start_time) continue;
    const d = new Date(bucket.start_time * 1000).toISOString().slice(0, 10);
    for (const row of bucket.results || []) {
      const model = String(row.model ?? '');
      if (model.startsWith('ft:') && (row.num_model_requests || 0) > 0) {
        last = last === null || d > last ? d : last;
      }
    }
  }
  if (last === null) return 'none-in-window';
  return -daysLeft(today, last);
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project read key. This script only issues '
                  + 'GET requests and never submits a job');
    process.exitCode = 2;
    return;
  }
  const today = (process.env.TODA || "dummy-toda")Y || new Date().toISOString().slice(0, 10);
  let findings = 0;

  const jobs = await allJobs(key);
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  const since = admin ? await daysSinceFtInference(admin, today) : null;
  const sinceText = since === null ? 'unknown'
    : since === 'none-in-window' ? 'not in the window' : `${since} day(s) ago`;
  console.log(`create: ${jobs.length} job(s) in the list, last ft: inference ${sinceText}`);

  const [state, detail] = createEligibility(today, jobs.length > 0, since);
  console.log(`${state.padEnd(28)} ${detail}`);
  for (const line of repairLines(state)) console.log(`  repair: ${line}`);
  if (FINDINGS.has(state)) findings += 1;

  const succeeded = jobs.filter((j) => String(j.status) === 'succeeded' && j.fine_tuned_model);
  console.log(`serve: ${succeeded.length} succeeded job(s)`);
  for (const job of succeeded) {
    const ftm = job.fine_tuned_model;
    const base = job.model;
    const [family, replacement] = familyFor(base);
    const [, ftmBody] = await getJson(`/models/${ftm}`, key);
    let shutdown = ftmBody.shutdown_date;
    if (!shutdown && base) {
      const [, baseBody] = await getJson(`/models/${base}`, key);
      shutdown = baseBody.shutdown_date;
    }
    const [deadline, source] = servingDeadline(shutdown, family);
    const [jstate, jdetail] = jobVerdict(job.status, ftm, deadline, today);
    console.log(`  ${String(ftm).padEnd(40)} ${(deadline || '---').padEnd(11)} `
                + `${source.padEnd(16)} ${jstate.padEnd(13)} ${jdetail}`);
    for (const line of repairLines(jstate, replacement)) console.log(`    repair: ${line}`);
    if (FINDINGS.has(jstate)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
