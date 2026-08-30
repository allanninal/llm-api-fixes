/**
 * Find an OpenAI organization with no project boundary to enforce anything on.
 *
 * Read only. Two paged GETs plus one for key NAMES. No request body is built,
 * no key value is read or printed.
 *
 * The finding is the absence of a boundary rather than the concentration of
 * spend: a single active project holds 100% by construction, and a dominant
 * project in an org that has nine is a different reading with a different
 * repair.
 */
const API = 'https://api.openai.com/v1';
const UNGROUPED = 'ungrouped';

// Whole-token matches only. Substring matching reports "devops-runner" as a
// development key and "provider-proxy" as a production one.
const ENV_WORDS = {
  prod: 'prod', production: 'prod', live: 'prod',
  stage: 'staging', staging: 'staging', preprod: 'staging',
  dev: 'dev', development: 'dev',
  local: 'local', laptop: 'local',
  test: 'test', testing: 'test', qa: 'test',
  ci: 'ci', build: 'ci',
  sandbox: 'sandbox', scratch: 'sandbox', playground: 'sandbox',
};

const FINDINGS = new Set(['no-boundary', 'boundary-unused']);

const money = (n) => Number(n).toLocaleString('en-US',
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Projects that can still receive traffic. Pure. Archived drops on either signal. */
export function active(projects) {
  return (projects ?? []).filter((project) => {
    const row = project ?? {};
    if (String(row.status ?? '').trim().toLowerCase() === 'archived') return false;
    if (row.archived_at) return false;
    return true;
  });
}

/** {project_id: dollars} from the cost report. Pure. Null project_id is ungrouped. */
export function spendByProject(buckets) {
  const rows = {};
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const name = String(result?.project_id ?? UNGROUPED);
      const value = Number(result?.amount?.value ?? 0);
      if (!Number.isFinite(value)) continue;
      rows[name] = (rows[name] ?? 0) + value;
    }
  }
  return rows;
}

/** [[project_id, dollars, share]] over real projects only. Pure. */
export function shares(spend) {
  const rows = Object.entries(spend ?? {}).filter(([k]) => k !== UNGROUPED);
  const total = rows.reduce((a, [, v]) => a + v, 0);
  const out = rows.map(([k, v]) => [k, Math.round(v * 100) / 100,
                                    total > 0 ? v / total : 0]);
  out.sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
  return out;
}

/** The environment classes named in one identifier. Pure. Whole tokens only. */
export function environments(name) {
  const tokens = String(name ?? '').trim().toLowerCase().split(/[^a-z0-9]+/);
  return new Set(tokens.filter((t) => ENV_WORDS[t]).map((t) => ENV_WORDS[t]));
}

/** Every environment class named across a set of identifiers. Pure. */
export function mixed(names) {
  const found = new Set();
  for (const name of names ?? []) for (const e of environments(name)) found.add(e);
  return found;
}

/** Classify the organization's topology. Pure. Returns [state, detail]. */
export function verdict(activeCount, ranked, minSpend = 1.0, dominant = 0.95) {
  const rows = [...(ranked ?? [])];
  const total = Math.round(rows.reduce((a, r) => a + r[1], 0) * 100) / 100;

  if (activeCount <= 0) {
    return ['no-active-projects',
            'the listing returned no active project at all, which usually means '
            + 'the key could not see them rather than that none exist'];
  }
  if (activeCount === 1) {
    return ['no-boundary',
            `1 active project holds 100% of $${money(total)}. There is no second `
            + 'container to cap, alert on, rate limit or attribute against.'];
  }
  if (total < minSpend) {
    return ['no-spend-yet',
            `${activeCount} active project(s) and $${money(total)} of attributable `
            + 'spend in the window. The boundary exists and nothing has tested it yet.'];
  }

  const [topId, , topShare] = rows[0];
  const quiet = rows.slice(1).filter((r) => r[1] <= 0);
  if (topShare >= dominant && quiet.length === rows.length - 1) {
    return ['boundary-unused',
            `${activeCount} active project(s), and ${topId} carries `
            + `${(topShare * 100).toFixed(0)}% of $${money(total)} while every other `
            + 'project has no spend at all. The containers exist and no traffic '
            + 'routes to them, so the controls on them enforce nothing.'];
  }
  if (topShare >= dominant) {
    return ['concentration-not-topology',
            `${activeCount} active project(s), and ${topId} carries `
            + `${(topShare * 100).toFixed(0)}% of $${money(total)}. This organization `
            + 'has a boundary, so that is a concentration reading rather than a '
            + 'topology one and has a different repair.'];
  }
  return ['separated',
          `${activeCount} active project(s) sharing $${money(total)}, top project at `
          + `${(topShare * 100).toFixed(0)}%`];
}

/** The repair for one topology verdict. Pure. Printed, never performed. */
export function repairLines(state, envs = []) {
  const found = [...envs].sort();
  if (state === 'no-boundary') {
    const lines = [
      'create prod, staging and dev with POST /v1/organization/projects, which is '
      + 'the smallest split that lets any control differ.',
      'give each project its own service account and key, then move traffic one '
      + 'key at a time rather than in one cutover.',
      'spend limits, spend alerts, rate limits, model permissions and data '
      + 'retention are all configured per project and cannot differ until the '
      + 'projects do.',
      'projects can be archived but never deleted, so the names are permanent. '
      + 'Spend ten minutes on them once.',
    ];
    if (found.length) {
      lines.unshift(`the environments already exist in your key names (${found.join(', ')}); `
        + 'they are simply not represented in the platform.');
    }
    return lines;
  }
  if (state === 'boundary-unused') {
    return [
      'the projects are not the problem. Nothing routes to them.',
      'issue a key in the quiet projects and move the traffic that belongs there, '
      + 'then set the limits per project afterwards.',
      'until traffic actually lands in a project, every control configured on it '
      + 'is inert.',
    ];
  }
  if (state === 'concentration-not-topology') {
    return ['do not restructure on this reading. Rank the cost rows by share of '
            + 'total and ask which line item is expensive instead.'];
  }
  return [];
}

/** Unix seconds at midnight UTC, `days` ago. Pure given `now`. */
export function windowStart(days, now = new Date()) {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return Math.floor(midnight / 1000) - days * 86400;
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/organization/* needs an `
                    + 'organization admin key, not a project key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* paged(key, path, params) {
  const q = { ...params };
  for (;;) {
    const page = await read(key, path, q);
    const data = page.data ?? [];
    for (const item of data) yield item;
    if (!page.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function* costBuckets(key, params, maxPages = 40) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, '/organization/costs', q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q.page = page.next_page;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key; a project '
                  + 'key cannot read /v1/organization/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);

  const projects = [];
  for await (const p of paged(admin, '/organization/projects',
                              { limit: 100, include_archived: 'true' })) {
    projects.push(p);
  }
  const live = active(projects);

  const buckets = [];
  for await (const b of costBuckets(admin, {
    start_time: windowStart(days), bucket_width: '1d',
    limit: Math.min(days, 30), group_by: 'project_id',
  })) buckets.push(b);

  const spend = spendByProject(buckets);
  const ranked = shares(spend);
  const total = Math.round(ranked.reduce((a, r) => a + r[1], 0) * 100) / 100;

  console.log(`${live.length} active project(s), ${projects.length - live.length} `
              + `archived, $${money(total)} in the last ${days} day(s)`);
  if (spend[UNGROUPED]) {
    console.log(`$${money(spend[UNGROUPED])} of cost came back ungrouped and is not `
                + 'counted as a project');
  }

  let envs = new Set();
  if (live.length) {
    const byId = new Map(live.map((p) => [p.id, p]));
    const target = byId.get(ranked[0]?.[0]) ?? live[0];
    const names = [];
    for await (const k of paged(admin, `/organization/projects/${target.id}/api_keys`,
                                { limit: 100, owner_project_access: 'any' })) {
      names.push(k?.name ?? '');
    }
    envs = mixed(names);
    if (envs.size) {
      console.log(`key names in ${target.name ?? target.id} already name ${envs.size} `
                  + `environment(s): ${[...envs].sort().join(', ')}`);
    }
  }

  const [state, detail] = verdict(live.length, ranked);
  console.log(`${state.padEnd(26)} ${detail}`);
  for (const line of repairLines(state, envs)) console.log(`  repair: ${line}`);
  process.exitCode = FINDINGS.has(state) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
