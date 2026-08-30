/**
 * Report OpenAI API keys whose owner no longer has access to the project.
 *
 * Read only. GET requests and nothing else, and it needs an ORGANIZATION ADMIN
 * key (sk-admin-...) because /v1/organization/* rejects project keys. An admin
 * key provisioned read-only is enough. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;

// Worst first, so the report leads with the key still serving traffic.
const SEVERITY = { serving: 4, orphaned: 3, unknown: 2, dormant: 1, 'in-force': 0 };

/** Best identity available for whoever owns a key. Pure. */
export function ownerLabel(key) {
  const owner = key.owner ?? {};
  const user = owner.user ?? {};
  const account = owner.service_account ?? {};
  return user.email || user.name || account.name || owner.type || 'unknown owner';
}

/**
 * Classify one organization.project.api_key object. Pure, so the rules can be
 * tested without an admin credential and without a network.
 */
export function verdict(key, now, hotDays = 7) {
  const raw = key.owner_project_access;
  if (raw === undefined || raw === null) {
    return ['unknown',
      'no owner_project_access on this object: ask for it explicitly with ' +
      'owner_project_access=any and re-read, rather than taking the absence ' +
      'for active'];
  }
  const access = String(raw).trim().toLowerCase();
  if (access === 'active') return ['in-force', 'owner still has access to this project'];
  if (access !== 'inactive') {
    return ['unknown', `unrecognised owner_project_access ${JSON.stringify(raw)}`];
  }

  const last = key.last_used_at;
  if (last === undefined || last === null) {
    return ['dormant',
      'owner has lost project access and this key has never authenticated a ' +
      'request. Nothing depends on it, so it is the safe one to revoke first.'];
  }
  const age = Math.floor((Number(now) - Number(last)) / DAY);
  if (age <= hotDays) {
    return ['serving',
      `owner has lost project access and the key authenticated a request ${age} ` +
      'day(s) ago. Something in production is still holding it: re-issue before ' +
      'you revoke.'];
  }
  return ['orphaned', `owner has lost project access; last used ${age} day(s) ago`];
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

  const hotDays = Number((process.env.HOT_DAYS || "dummy-hot-days") ?? 7);
  const scope = (process.env.ALL_KEYS || "dummy-all-keys") ? 'any' : 'inactive';
  const now = Math.floor(Date.now() / 1000);

  const rows = [];
  let projects = 0;
  // include_archived=true: an archived project still holds live keys and is
  // absent from the default listing.
  for await (const project of paged(adminKey, '/organization/projects',
                                    { include_archived: 'true' })) {
    projects += 1;
    const path = `/organization/projects/${project.id}/api_keys`;
    for await (const key of paged(adminKey, path, { owner_project_access: scope })) {
      const [state, detail] = verdict(key, now, hotDays);
      rows.push({ state, detail, project, key });
    }
  }

  rows.sort((a, b) =>
    (SEVERITY[b.state] ?? 2) - (SEVERITY[a.state] ?? 2) ||
    (b.key.last_used_at ?? 0) - (a.key.last_used_at ?? 0));

  let bad = 0;
  for (const { state, detail, project, key } of rows) {
    const line = `${state.padEnd(9)} ${project.name ?? project.id} / ` +
                 `${ownerLabel(key)}  ${key.redacted_value ?? '?'}  ${detail}`;
    if (state === 'in-force') { console.log(line); continue; }
    bad += 1;
    console.warn(line);
    console.warn('  repair: mint a replacement under a service account, deploy it, ' +
                 'confirm last_used_at stops moving, then remove this one: ' +
                 `DELETE ${API}/organization/projects/${project.id}/api_keys/${key.id}`);
  }

  console.log(`${rows.length} key(s) read across ${projects} project(s), ${bad} ` +
              'whose owner no longer has project access');
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main(), fail on the missing key, and set a non-zero exit code
// that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
