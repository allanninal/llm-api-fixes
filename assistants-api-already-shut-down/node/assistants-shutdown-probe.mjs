/**
 * Probe an endpoint family that is already past its published shutdown date.
 *
 * Read only. Every request is a GET: the assistants listing, a control listing
 * of models on the same key, and the organization usage report. Nothing here
 * creates an assistant, a thread or a run.
 *
 * Past a shutdown date the polarity inverts: a 404 is the expected answer and
 * a 200 is the finding. A 404 on its own cannot tell a closed path from a key
 * that reads nothing, so the unit is a pair with the path as the only variable.
 */
export const API = 'https://api.openai.com/v1';
export const SUBJECT = '/assistants';
export const CONTROL = '/models';

// Announced 26 August 2025 with a year of notice. Published, not readable.
export const SHUTDOWN = '2026-08-26';

// A 429 is a refusal from something that exists, which is not a 404.
const LIVE = new Set(['answering', 'throttled']);

const FINDINGS = new Set(['grace-access', 'shut-down', 'closed-early',
  'control-failed', 'unreadable', 'cliff-on-the-date', 'dip-on-the-date']);

const REPAIRS = {
  'grace-access':
    `this organization still reaches an API that shut down on ${SHUTDOWN}. That `
    + 'is grace, not support, and it has no expiry you can read. Move it now: '
    + 'runs become POST /v1/responses carrying a conversation id, threads become '
    + 'POST /v1/conversations, and the OpenAI-Beta header is deleted.',
  'shut-down':
    'runs become POST /v1/responses carrying a conversation id from '
    + 'POST /v1/conversations, and the OpenAI-Beta: assistants=v2 header is '
    + 'deleted. There is no model id to swap here, which is why checking the '
    + 'model id first never helps.',
  'closed-early':
    'the path is already gone and the published date has not arrived. Treat '
    + 'the date as the outside edge rather than the schedule.',
  'control-failed':
    'the control path did not answer either, so nothing was proved about the '
    + 'subject path. Fix the credential or the network and re-run.',
  'cliff-on-the-date':
    "this project's traffic stopped on the shutdown date, so the outage is the "
    + 'closure and not a deploy. Migrate this project first.',
  'dip-on-the-date':
    "part of this project's traffic stopped on the shutdown date. The project "
    + 'serves other work as well, so the assistants share is what needs '
    + 'migrating, not the whole project.',
};

const day = (iso) => Date.parse(`${iso}T00:00:00Z`);

/** Whole days from a published date to today. Pure. Negative before it. */
export function daysPast(today, when = SHUTDOWN) {
  return Math.round((day(String(today)) - day(String(when))) / 86400000);
}

/** What one listing's status means on its own. Pure. [state, why]. */
export function probeState(status, body = null) {
  if (status === null || status === undefined) {
    return ['unreachable', 'no response at all from this path'];
  }
  const s = Number(status);
  const b = (body && typeof body === 'object') ? body : {};
  if (s === 200) {
    const kind = b.object || 'a body with no object field';
    return ['answering', `200, and the response is ${kind}`];
  }
  const err = (b.error && typeof b.error === 'object') ? b.error : {};
  const code = err.code || err.type || 'no error code';
  if (s === 404) return ['gone', `404 ${code}, which is what a closed path returns`];
  if (s === 401 || s === 403) {
    return ['credentials', `${s} ${code}, so this probe says nothing about the path`];
  }
  if (s === 429) {
    return ['throttled', `429 ${code}, which is a refusal from a path that still routes`];
  }
  return ['refused', `${s} ${code}`];
}

/** Grade the subject path against the control path. Pure. [state, why]. */
export function accessVerdict(subject, control, past) {
  if (!LIVE.has(control)) {
    return ['control-failed',
      `the control path came back ${control}, so this key proves nothing about `
      + 'the subject path'];
  }
  if (LIVE.has(subject)) {
    if (past >= 0) {
      return ['grace-access',
        `the subject path answered ${past} day(s) after its published shutdown `
        + 'date, which is access on grace rather than a supported state'];
    }
    return ['still-open',
      `the subject path answers and the shutdown is ${-past} day(s) away`];
  }
  if (subject === 'gone') {
    if (past >= 0) {
      return ['shut-down',
        'the control path answers and the subject path does not, so this '
        + `organization is past the ${SHUTDOWN} shutdown`];
    }
    return ['closed-early',
      `the subject path is already gone with ${-past} day(s) still to run on `
      + 'the published date'];
  }
  return ['unreadable',
    `the subject path came back ${subject}, which is neither an answer nor a closure`];
}

/** Grade a daily [[date, requests]] series. Pure. [state, why]. */
export function cliffVerdict(series, when = SHUTDOWN) {
  const rows = (series || [])
    .map(([d, n]) => [String(d), Number(n) || 0])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  if (!rows.length) {
    return ['not-checked', 'no usage buckets were read, so the outage could not be dated'];
  }
  const before = rows.filter(([d]) => d < String(when)).map(([, n]) => n);
  const after = rows.filter(([d]) => d >= String(when)).map(([, n]) => n);
  if (!before.length || !after.length) {
    return ['window-too-short',
      `the window does not span ${when}, so there is nothing to compare across it`];
  }
  const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const meanBefore = mean(before);
  const meanAfter = mean(after);
  if (meanBefore === 0) {
    return ['no-traffic-in-window',
      `this project had no requests before ${when} either, so there is no `
      + 'outage here to explain'];
  }
  if (meanAfter === 0) {
    const live = rows.filter(([, n]) => n > 0).map(([d]) => d);
    const lastLive = live.length ? live[live.length - 1] : null;
    const eve = new Date(day(String(when)) - 86400000).toISOString().slice(0, 10);
    if (lastLive === eve) {
      return ['cliff-on-the-date',
        `${meanBefore.toFixed(0)} requests/day until ${lastLive} and none from `
        + `${when}, which is the shutdown and not a deploy`];
    }
    return ['cliff-elsewhere',
      `traffic stopped, but the last live day is ${lastLive} rather than ${eve}, `
      + `the day before ${when}`];
  }
  const share = meanAfter / meanBefore;
  if (share <= 0.5) {
    return ['dip-on-the-date',
      `requests fell to ${(share * 100).toFixed(0)}% of the prior mean on ${when}, `
      + 'so part of this project was assistants traffic and part was not'];
  }
  return ['still-running',
    `requests continued across ${when} at ${(share * 100).toFixed(0)}% of the prior mean`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const line = REPAIRS[state];
  if (!line) return [];
  if (state === 'grace-access' || state === 'shut-down') {
    return [line,
      'the migration guide is Migrate to the Responses API. There is no '
      + 'successor model id, so no config change closes this.'];
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

async function usageSeries(key, days) {
  const start = Math.floor(Date.now() / 1000) - days * 86400;
  const [status, body] = await getJson('/organization/usage/completions', key, {
    start_time: start,
    bucket_width: '1d',
    'group_by[]': ['project_id'],
    limit: Math.max(7, Math.min(days, 180)),
  });
  if (status !== 200) {
    console.log(`usage report came back ${status}, so no outage can be dated`);
    return {};
  }
  const out = {};
  for (const bucket of body.data || []) {
    if (!bucket.start_time) continue;
    const d = new Date(bucket.start_time * 1000).toISOString().slice(0, 10);
    for (const row of bucket.results || []) {
      const pid = row.project_id || '(unattributed)';
      (out[pid] ||= []).push([d, row.num_model_requests || 0]);
    }
  }
  return out;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project read key. This script only '
                  + 'issues GET requests');
    process.exitCode = 2;
    return;
  }
  const today = (process.env.TODA || "dummy-toda")Y || new Date().toISOString().slice(0, 10);
  const days = Number((process.env.DAY || "dummy-day")S || 30);
  const past = daysPast(today);
  console.log(`shutdown ${SHUTDOWN}, ${Math.abs(past)} day(s) ${past >= 0 ? 'past' : 'away'}`);

  const states = {};
  for (const [role, path] of [['control', CONTROL], ['subject', SUBJECT]]) {
    const [status, body] = await getJson(path, key, { limit: 1 });
    const [state, why] = probeState(status, body);
    states[role] = state;
    console.log(`  ${role.padEnd(8)} GET /v1${path.padEnd(12)} ${status ?? '---'}  ${state.padEnd(12)} ${why}`);
  }

  let findings = 0;
  const [state, why] = accessVerdict(states.subject, states.control, past);
  console.log(`${state.padEnd(20)} ${why}`);
  for (const line of repairLines(state)) console.log(`  repair: ${line}`);
  if (FINDINGS.has(state)) findings += 1;

  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.log(`${'not-dated'.padEnd(20)} no admin key, so the outage was observed and not dated`);
  } else {
    const series = await usageSeries(admin, days);
    if (!Object.keys(series).length) {
      console.log(`${'not-dated'.padEnd(20)} the usage report returned nothing to date it with`);
    }
    for (const pid of Object.keys(series).sort()) {
      const [cstate, cwhy] = cliffVerdict(series[pid]);
      console.log(`${pid.padEnd(20)} ${cstate.padEnd(18)} ${cwhy}`);
      for (const line of repairLines(cstate)) console.log(`  repair: ${line}`);
      if (FINDINGS.has(cstate)) findings += 1;
    }
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
