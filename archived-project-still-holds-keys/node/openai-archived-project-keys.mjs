/**
 * Report live API keys sitting inside archived OpenAI projects.
 *
 * Read only. GET requests and nothing else, with an ORGANIZATION ADMIN key
 * (sk-admin-...) because /v1/organization/* rejects project keys; read-only
 * admin scopes are enough. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;
const TRUTHY = ['true', '1', 'yes', 'on'];

/**
 * True when a projects listing will actually include archived projects. Pure.
 * include_archived defaults to false, so an audit that never passes it reports
 * a clean result over a subset of the organization.
 */
export function coversArchived(params = {}) {
  const value = params.include_archived;
  if (typeof value === 'boolean') return value;
  if (value === undefined || value === null) return false;
  return TRUTHY.includes(String(value).trim().toLowerCase());
}

/**
 * Classify one project against the keys found inside it. Pure. All timestamps
 * are unix seconds; last_used_at is null on a key that has never been used.
 */
export function verdict(project, keys, now) {
  const status = String(project.status ?? '').trim().toLowerCase();
  const archivedAt = project.archived_at;
  if (status !== 'archived' && (archivedAt === undefined || archivedAt === null)) {
    return ['active', 'not archived; outside the scope of this check'];
  }

  const all = [...(keys ?? [])];
  if (all.length === 0) return ['clean', 'archived, and holds no API keys'];

  const usedAfter = all.filter((k) => k.last_used_at && archivedAt &&
                                      Number(k.last_used_at) > Number(archivedAt));
  if (usedAfter.length > 0) {
    const newest = Math.max(...usedAfter.map((k) => Number(k.last_used_at)));
    const days = Math.floor((Number(now) - newest) / DAY);
    return ['still-serving',
      `${usedAfter.length} of ${all.length} live key(s) authenticated a request ` +
      `after the project was archived, the most recent ${days} day(s) ago. This ` +
      'project is closed on paper and running in fact.'];
  }

  const everUsed = all.filter((k) => k.last_used_at);
  if (everUsed.length > 0) {
    const newest = Math.max(...everUsed.map((k) => Number(k.last_used_at)));
    const days = Math.floor((Number(now) - newest) / DAY);
    return ['live-keys',
      `${all.length} live key(s) inside an archived project, last used ${days} ` +
      'day(s) ago. Nothing has needed them since the archive.'];
  }
  return ['dormant-keys',
    `${all.length} live key(s) inside an archived project, none of which has ` +
    'ever authenticated a request'];
}

async function get(adminKey, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${adminKey}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: /v1/organization/* needs an organization ' +
                    'admin key (sk-admin-...), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function* paged(adminKey, path, params = {}) {
  const q = { limit: 100, ...params };
  for (;;) {
    const page = await get(adminKey, path, q);
    const data = page.data ?? [];
    for (const item of data) yield item;
    if (!page.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1].id;
  }
}

async function main() {
  const adminKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!adminKey) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key (sk-admin-...); ' +
                  'a project key cannot read /v1/organization/*');
    process.exitCode = 2;
    return;
  }
  const now = Math.floor(Date.now() / 1000);

  const listing = { limit: 100, include_archived: 'true' };
  console.log(`listing covers archived projects: ${
    coversArchived(listing) ? 'yes' : 'NO, this audit is partial'}`);

  const projects = [];
  for await (const p of paged(adminKey, '/organization/projects', listing)) projects.push(p);

  let archived = 0;
  let exposed = 0;
  for (const project of projects) {
    let keys = [];
    const isArchived = String(project.status ?? '').toLowerCase() === 'archived' ||
                       (project.archived_at !== undefined && project.archived_at !== null);
    if (isArchived) {
      archived += 1;
      for await (const k of paged(adminKey,
                                  `/organization/projects/${project.id}/api_keys`,
                                  { owner_project_access: 'any' })) keys.push(k);
    }
    const [state, detail] = verdict(project, keys, now);
    const line = `${state.padEnd(13)} ${project.name ?? project.id}  ${detail}`;
    if (state === 'active' || state === 'clean') {
      if (state === 'clean') console.log(line);
      continue;
    }
    exposed += keys.length;
    console.warn(line);
    for (const key of keys) {
      console.warn(`  repair: DELETE ${API}/organization/projects/${project.id}` +
                   `/api_keys/${key.id}  (${key.redacted_value ?? key.name ?? 'unnamed'})`);
    }
    console.warn(`  and check the spend: GET ${API}/organization/costs` +
                 '?start_time=<now-30d>&group_by=project_id');
  }

  console.log(`${projects.length} project(s), ${archived} archived, ${exposed} ` +
              'live key(s) inside them');
  process.exitCode = exposed ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
