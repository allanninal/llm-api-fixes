/**
 * Reconcile OpenAI's token totals against the ones your own telemetry recorded.
 *
 * Read only. Two GET requests against the organization endpoints and a JSON
 * file you supply. Those endpoints reject project keys, so this needs an
 * organization admin key (sk-admin-), provisioned read-only.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

const FINDINGS = ['undercount', 'overcount', 'untracked', 'phantom'];

/**
 * Fold usage buckets into one row per project. Pure. Requests ride along
 * because OpenAI reports them and Anthropic does not.
 */
export function apiTotals(buckets) {
  const rows = new Map();
  for (const bucket of buckets ?? []) {
    for (const result of bucket.results ?? []) {
      const project = String(result.project_id ?? 'unknown');
      const row = rows.get(project) ?? { tokens: 0, requests: 0 };
      row.tokens += (Number(result.input_tokens ?? 0) || 0)
                  + (Number(result.output_tokens ?? 0) || 0);
      row.requests += Number(result.num_model_requests ?? 0) || 0;
      rows.set(project, row);
    }
  }
  return rows;
}

/**
 * Read one project's own recorded token count. Pure. Returns a number, or null
 * when nothing was recorded for that project at all. Zero means your pipeline
 * saw the project and recorded nothing; null means it has never heard of it,
 * and those are two different bugs with two different owners.
 */
export function recordedTokens(entry) {
  if (entry === null || entry === undefined) return null;
  if (typeof entry === 'boolean') return null;
  if (typeof entry === 'number') return Number.isFinite(entry) ? Math.trunc(entry) : null;
  if (typeof entry === 'object' && !Array.isArray(entry)) {
    if ('tokens' in entry) {
      const value = Number(entry.tokens ?? 0);
      return Number.isFinite(value) ? Math.trunc(value) : null;
    }
    if ('input_tokens' in entry || 'output_tokens' in entry) {
      const value = (Number(entry.input_tokens ?? 0) || 0)
                  + (Number(entry.output_tokens ?? 0) || 0);
      return Number.isFinite(value) ? Math.trunc(value) : null;
    }
  }
  return null;
}

/**
 * Compare one project's two numbers. Pure. Returns [state, detail]. Three
 * disagreements, not one: short is the streaming gap, over is double counting,
 * and absent from the telemetry is neither.
 */
export function compare(apiTokens, recorded, tolerance = 0.05, minTokens = 100000) {
  const api = Number(apiTokens) || 0;

  if (api <= 0) {
    if (recorded === null || recorded === undefined || Number(recorded) <= 0) {
      return ['idle', 'no usage in the org report and none recorded'];
    }
    return ['phantom',
      `${Math.trunc(Number(recorded))} token(s) recorded against a project the ` +
      'org report shows no usage for. That is a project id mapping, not a ' +
      'streaming problem.'];
  }

  if (recorded === null || recorded === undefined) {
    return ['untracked',
      `${api} token(s) in the org report and no telemetry for this project at ` +
      'all. Not an undercount: nothing here is being recorded.'];
  }

  const seen = Math.trunc(Number(recorded));
  if (api < minTokens) {
    return ['too-little-traffic',
      `${api} token(s) in the window, too few for the comparison to mean anything`];
  }

  const gap = api - seen;
  const share = gap / api;
  if (share > tolerance) {
    return ['undercount',
      `recorded ${seen} token(s) against ${api} in the org report, short by ` +
      `${gap} (${(share * 100).toFixed(1)}%). Streamed responses report usage: ` +
      'null unless the request asked for the totals.'];
  }
  if (share < -tolerance) {
    return ['overcount',
      `recorded ${seen} token(s) against ${api} in the org report, over by ` +
      `${-gap} (${(-share * 100).toFixed(1)}%). Recording more than you were ` +
      'billed for is double counting, not a streaming gap.'];
  }
  return ['matched',
    `recorded ${seen} token(s) against ${api} in the org report ` +
    `(${(Math.abs(share) * 100).toFixed(1)}% apart)`];
}

/**
 * Pro-rata dollars behind an untracked token gap. Pure. An estimate: input and
 * output are priced differently, so this is only exact when the missing traffic
 * has the same mix as the rest. Read from the cost report, not a price table.
 */
export function untrackedCost(costBuckets, projectId, apiTokens, gapTokens) {
  const api = Number(apiTokens) || 0;
  const gap = Number(gapTokens) || 0;
  if (api <= 0 || gap <= 0) return 0;
  let spend = 0;
  for (const bucket of costBuckets ?? []) {
    for (const result of bucket.results ?? []) {
      if (String(result.project_id ?? '') !== String(projectId)) continue;
      spend += Number(result.amount?.value ?? 0) || 0;
    }
  }
  return Math.round(spend * Math.min(1, gap / api) * 100) / 100;
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.forEach((one) => url.searchParams.append(k, String(one)));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function pages(key, path, params, maxPages = 40) {
  const out = [];
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, path, query);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) break;
    query = { ...params, page: page.next_page };
  }
  return out;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key") ?? (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key, read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }

  const flag = process.argv.find((a) => a.startsWith('--telemetry='));
  const path = flag ? flag.slice('--telemetry='.length) : (process.env.TELEMETRY || "dummy-telemetry");
  if (!path) {
    console.error('pass --telemetry=week.json (your own recorded token counts, ' +
                  'keyed by project id)');
    process.exitCode = 2;
    return;
  }

  let telemetry;
  try {
    telemetry = JSON.parse(await readFile(path, 'utf8'));
  } catch (err) {
    console.error(`could not read ${path}: ${err.message}`);
    process.exitCode = 2;
    return;
  }
  if (telemetry === null || typeof telemetry !== 'object' || Array.isArray(telemetry)) {
    console.error(`${path} should be a JSON object keyed by project id`);
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const tolerance = Number((process.env.TOLERANCE || "dummy-tolerance") ?? 0.05);
  const minTokens = Number((process.env.MIN_TOKENS || "dummy-min-tokens") ?? 100000);
  const showAll = process.argv.includes('--show-all');

  const start = Math.floor(Date.now() / 1000) - days * 86400;
  const usage = await pages(key, '/organization/usage/completions', {
    start_time: start,
    bucket_width: '1d',
    limit: Math.min(31, Math.max(1, days)),
    group_by: ['project_id'],
  });
  const costs = await pages(key, '/organization/costs', {
    start_time: start,
    bucket_width: '1d',
    limit: Math.min(180, Math.max(1, days)),
    group_by: ['project_id'],
  });

  const rows = apiTotals(usage);
  for (const project of Object.keys(telemetry)) {
    if (!rows.has(project)) rows.set(project, { tokens: 0, requests: 0 });
  }
  if (rows.size === 0) {
    console.log(`no completions usage in the last ${days} day(s) and nothing in ` +
                'the telemetry file');
    return;
  }

  let found = 0;
  for (const project of [...rows.keys()].sort()) {
    const apiTokens = rows.get(project).tokens;
    const recorded = recordedTokens(telemetry[project]);
    const [state, detail] = compare(apiTokens, recorded, tolerance, minTokens);
    const line = `${state.padEnd(18)} ${project}  ${detail}`;

    if (FINDINGS.includes(state)) {
      found += 1;
      console.warn(line);
      if (state === 'undercount') {
        const gap = apiTokens - (recorded ?? 0);
        const money = untrackedCost(costs, project, apiTokens, gap);
        console.warn(`  about $${money.toFixed(2)} of this project's spend over ` +
          `${days} day(s) is not in your own numbers`);
        console.warn('  repair: set stream_options include_usage on every ' +
          'streaming Chat Completions call and read the final chunk, or read ' +
          'response.usage from the terminal response.completed event on the ' +
          'Responses API. Streams the client abandons will still lose theirs.');
      } else if (state === 'overcount') {
        console.warn('  repair: this is double counting rather than a streaming ' +
          'gap. Look for retries recorded once per attempt, or one response ' +
          'written by two consumers.');
      } else if (state === 'untracked') {
        console.warn('  repair: this project is absent from your telemetry. Map ' +
          'the project id before treating any of these numbers as a margin.');
      } else {
        console.warn('  repair: your telemetry attributes tokens to a project ' +
          'the organization report has no usage for. Check the project id, not ' +
          'the streaming client.');
      }
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${rows.size} project(s) reconciled, ${found} with a gap`);
  process.exitCode = found ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
