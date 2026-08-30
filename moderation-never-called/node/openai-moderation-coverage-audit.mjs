/**
 * Find an OpenAI organization whose moderation endpoint is never called.
 *
 * Read only. Two paged GETs against /v1/organization/usage/moderations and
 * /v1/organization/usage/completions. No request body is constructed.
 *
 * The script deliberately does not call the moderations endpoint to prove it
 * works: sending content to a model is generating. The finding comes from two
 * request counts the organization already has.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;

const RETIRED_PREFIX = 'text-moderation';
const CURRENT = 'omni-moderation-latest';

const FINDINGS = new Set(['never-called', 'retired-model-id', 'thin-coverage']);
const SEVERITY = { 'never-called': 0, 'retired-model-id': 1, 'thin-coverage': 2 };

/** {project_id: {requests, models}} across buckets. Pure. Zero rows create nothing. */
export function fold(buckets, countField = 'num_model_requests') {
  const out = {};
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const n = Math.trunc(Number(result?.[countField] ?? 0));
      if (!Number.isFinite(n) || n <= 0) continue;
      const pid = String(result?.project_id ?? 'unattributed');
      const model = String(result?.model ?? 'unknown');
      const entry = (out[pid] ??= { requests: 0, models: {} });
      entry.requests += n;
      entry.models[model] = (entry.models[model] ?? 0) + n;
    }
  }
  return out;
}

/** Is this a retired moderation model id? Pure. Prefix match, so pins count. */
export function isRetired(model) {
  return String(model ?? '').trim().toLowerCase().startsWith(RETIRED_PREFIX);
}

/** Sorted retired ids inside one {model: requests} mapping. Pure. */
export function retiredIds(models) {
  return Object.keys(models ?? {}).filter(isRetired).sort();
}

/** [[project, completions, moderations, models]] busiest first. Pure. */
export function coverage(completions, moderations) {
  const rows = [];
  for (const [pid, entry] of Object.entries(completions ?? {})) {
    const mod = (moderations ?? {})[pid] ?? {};
    rows.push([pid, Math.trunc(Number(entry?.requests ?? 0)),
               Math.trunc(Number(mod.requests ?? 0)), { ...(mod.models ?? {}) }]);
  }
  rows.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return rows;
}

/** Classify one coverage row. Pure. Model ids are tested before any count. */
export function classify(row, minCompletions = 500, minRatio = 0.2) {
  const [pid, completions, moderations, models] = row;
  if (completions < minCompletions) {
    return ['below-floor',
            `${completions} completion request(s), under the ${minCompletions} floor`];
  }
  const retired = retiredIds(models);
  if (retired.length) {
    const share = retired.reduce((a, m) => a + models[m], 0) / Math.max(1, moderations);
    return ['retired-model-id',
            `${moderations} moderation request(s), ${Math.round(share * 100)}% of `
            + `them on ${retired.join(', ')}`];
  }
  if (moderations <= 0) {
    return ['never-called',
            `${completions} completion request(s) and no moderation request at all`];
  }
  const ratio = moderations / completions;
  if (ratio < minRatio) {
    return ['thin-coverage',
            `${moderations} moderation request(s) against ${completions} completion `
            + `request(s), a ratio of ${ratio.toFixed(2)}`];
  }
  return ['covered', `${moderations} moderation request(s), ratio ${ratio.toFixed(2)}`];
}

/** The repair for one classified project. Pure. Printed, never performed. */
export function repairLines(state, row) {
  const [pid, , , models] = row;
  const lines = [];
  if (!FINDINGS.has(state)) return lines;
  if (state === 'never-called') {
    lines.push('route user input through the moderations endpoint before the '
      + 'completion. It bills nothing, so a round trip is the entire cost.');
    lines.push('branch on flagged, and log category_scores rather than the single '
      + 'boolean, so a threshold can be tuned per category later without another '
      + 'deploy.');
  } else if (state === 'retired-model-id') {
    lines.push(`move ${retiredIds(models).join(', ')} to ${CURRENT}, which is `
      + 'current and is the only moderation model that reads images as well as text.');
    lines.push('if this product accepts uploads, the retired id has been screening '
      + 'the text half only.');
  } else {
    lines.push('moderation is being called on a small share of the traffic. Find '
      + 'the call sites that skip it before tuning anything; the ratio alone cannot '
      + 'tell you which they are.');
  }
  lines.push(`re-read project ${pid} with the same two usage reports after the `
    + 'deploy, and check the model column, not only the count');
  return lines;
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const one of v) url.searchParams.append(k, String(one));
    else url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: the usage reports need an `
      + 'organization admin key with api.usage.read, not a project key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function usage(key, path, start, end) {
  const params = { start_time: start, end_time: end, bucket_width: '1d',
                   limit: 31, group_by: ['project_id', 'model'] };
  const out = [];
  for (;;) {
    const page = await read(key, path, params);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) return out;
    params.page = page.next_page;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key; a project '
                  + 'key cannot read /v1/organization/usage/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minCompletions = Number((process.env.MIN_COMPLETIONS || "dummy-min-completions") ?? 500);
  const minRatio = Number((process.env.MIN_RATIO || "dummy-min-ratio") ?? 0.2);
  const end = Math.floor(Date.now() / 1000);
  const start = end - Math.max(1, days) * DAY;

  const completions = fold(await usage(admin, '/organization/usage/completions', start, end));
  const moderations = fold(await usage(admin, '/organization/usage/moderations', start, end));

  const graded = coverage(completions, moderations)
    .map((row) => [row, classify(row, minCompletions, minRatio)]);
  const bad = graded.filter(([, [state]]) => FINDINGS.has(state));
  const overFloor = graded.filter(([, [state]]) => state !== 'below-floor').length;

  console.log(`${overFloor} project(s) over the ${minCompletions} request floor, `
              + `${bad.length} finding(s)`);

  bad.sort(([ra, [sa]], [rb, [sb]]) =>
    (SEVERITY[sa] ?? 9) - (SEVERITY[sb] ?? 9) || rb[1] - ra[1]);

  for (const [row, [state, detail]] of bad) {
    console.warn(`${state.padEnd(18)} ${row[0].padEnd(14)} ${detail}`);
    for (const line of repairLines(state, row)) console.warn(`  repair: ${line}`);
  }
  console.log('not graded: this report counts requests. Whether the code branched '
              + "on flagged, and whether the input assessed was the user's, are not "
              + 'in the API.');
  process.exitCode = bad.length ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
