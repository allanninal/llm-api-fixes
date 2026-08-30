/**
 * Find an OpenAI organization where the owner role is the default.
 *
 * Read only. Paged GETs against /v1/organization/users, /admin_api_keys,
 * /projects and each project's /users. No request body is constructed and no
 * key value is read or printed; email addresses are masked.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;

const OWNER = 'owner';
const READER = 'reader';
const OTHER = 'other';

const FINDINGS = new Set(['everyone-is-owner', 'owner-majority', 'owner-count-high']);

/** The roster with service accounts removed. Pure. */
export function humans(users) {
  return (users ?? []).filter((u) => !(u ?? {}).is_service_account);
}

/** Normalise one member's org role. Pure. Unknown roles are "other". */
export function roleOf(user) {
  const raw = String(user?.role ?? '').trim().toLowerCase();
  return raw === OWNER || raw === READER ? raw : OTHER;
}

/** {role: count} over a roster. Pure. */
export function roleCounts(people) {
  const counts = { [OWNER]: 0, [READER]: 0, [OTHER]: 0 };
  for (const person of people ?? []) counts[roleOf(person)] += 1;
  return counts;
}

/** Owners as a share of the roster. Pure. */
export function ownerRatio(counts) {
  const data = counts ?? {};
  const total = [OWNER, READER, OTHER].reduce((a, r) => a + (data[r] ?? 0), 0);
  if (total <= 0) return 0;
  return (data[OWNER] ?? 0) / total;
}

/** Hide the local part of an email address. Pure. Non-emails pass through. */
export function mask(email) {
  const text = String(email ?? '').trim();
  if (!text.includes('@')) return text || 'unknown';
  const at = text.indexOf('@');
  const local = text.slice(0, at);
  if (!local) return text;
  return `${local[0]}***${text.slice(at)}`;
}

/** Has this member authenticated an API request recently? Pure. A question, not a verdict. */
export function unusedPrivilege(user, now, days = 180) {
  const stamp = user?.api_key_last_used_at;
  if (!stamp) return [true, 'no API key use on record'];
  const value = Number(stamp);
  if (!Number.isFinite(value)) return [true, 'unreadable api_key_last_used_at'];
  const age = Math.floor((Number(now) - value) / DAY);
  return [age >= days, `last key use ${age} day(s) ago`];
}

/** {owner_id: owner_name} from the admin key listing. Pure. Owner block only. */
export function adminKeyOwners(keys) {
  const out = {};
  for (const key of keys ?? []) {
    const owner = key?.owner ?? {};
    const id = owner.id ?? owner.user?.id;
    if (!id) continue;
    out[String(id)] = String(owner.name ?? owner.user?.email ?? 'unnamed');
  }
  return out;
}

/** [owners, total, ratio] for one project's member list. Pure. */
export function projectOwnerShare(members) {
  const rows = (members ?? []).filter((m) => !(m ?? {}).is_service_account);
  const owners = rows.filter(
    (m) => String(m?.role ?? '').trim().toLowerCase() === OWNER).length;
  const total = rows.length;
  return [owners, total, total ? owners / total : 0];
}

/** Classify the roster. Pure. Returns [state, detail]. The member floor comes first. */
export function verdict(counts, minMembers = 3, ratioMax = 0.50, countMax = 5) {
  const data = counts ?? {};
  const owners = data[OWNER] ?? 0;
  const total = [OWNER, READER, OTHER].reduce((a, r) => a + (data[r] ?? 0), 0);
  const ratio = ownerRatio(data);

  if (total < minMembers) {
    return ['too-few-members',
            `${total} human member(s) in the organization, too few for a role `
            + 'distribution to mean anything'];
  }
  if (ratio >= 0.90 && owners >= 3) {
    return ['everyone-is-owner',
            `${owners} of ${total} human member(s) hold the owner role `
            + `(${(ratio * 100).toFixed(0)}%). The distinction between owner and `
            + 'reader has stopped existing here.'];
  }
  if (ratio > ratioMax) {
    return ['owner-majority',
            `${owners} of ${total} human member(s) hold the owner role `
            + `(${(ratio * 100).toFixed(0)}%)`];
  }
  if (owners > countMax) {
    return ['owner-count-high',
            `${owners} of ${total} human member(s) hold the owner role. The share `
            + `is fine and the absolute count is past the ${countMax} this audit `
            + 'treats as a working ceiling, which is a convention rather than a '
            + 'platform rule.'];
  }
  return ['scoped',
          `${owners} of ${total} human member(s) hold the owner role `
          + `(${(ratio * 100).toFixed(0)}%)`];
}

/** The repair for one roster verdict. Pure. Printed, never performed. */
export function repairLines(state, scimOwners = 0, keyHolders = 0, looseProjects = 0) {
  if (!FINDINGS.has(state)) return [];
  const lines = [
    'demote to reader anyone who does not administer billing, keys or projects, '
    + 'with POST /v1/organization/users/{user_id} and role reader.',
    'grant a project role instead, so people keep the access they actually use: '
    + 'POST /v1/organization/projects/{project_id}/users with member.',
  ];
  if (scimOwners) {
    lines.push(`${scimOwners} owner(s) are SCIM-managed. Change the group mapping in `
      + 'the identity provider; a role changed through this API is reverted at the '
      + 'next sync.');
  }
  if (keyHolders) {
    lines.push(`${keyHolders} owner(s) hold an admin API key. Revoke the key before `
      + 'the role, or the credential outlives the demotion.');
  }
  if (looseProjects) {
    lines.push(`${looseProjects} project(s) also grant owner to every member, so an `
      + 'org-level demotion alone will not change what anybody can do there.');
  }
  return lines;
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

async function paged(key, path, params) {
  const out = [];
  const q = { ...params };
  for (;;) {
    const page = await read(key, path, q);
    const data = page.data ?? [];
    out.push(...data);
    if (!page.has_more || data.length === 0) return out;
    q.after = page.last_id ?? data[data.length - 1]?.id;
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
  const days = Number((process.env.DAYS || "dummy-days") ?? 180);
  const now = Math.floor(Date.now() / 1000);

  const users = await paged(admin, '/organization/users', { limit: 100 });
  const people = humans(users);
  const counts = roleCounts(people);
  const owners = people.filter((p) => roleOf(p) === OWNER);
  const scim = owners.filter((p) => p.is_scim_managed);
  const holders = adminKeyOwners(
    await paged(admin, '/organization/admin_api_keys', { limit: 100 }));

  const projects = (await paged(admin, '/organization/projects', { limit: 100 }))
    .filter((p) => String(p.status ?? '').toLowerCase() !== 'archived');
  let loose = 0;
  for (const project of projects) {
    const members = await paged(admin, `/organization/projects/${project.id}/users`,
                                { limit: 100 });
    const [, total, ratio] = projectOwnerShare(members);
    if (total && ratio >= 0.90) loose += 1;
  }

  console.log(`${people.length} member(s), ${users.length - people.length} service `
              + `account(s) excluded, ${scim.length} SCIM-managed`);

  const [state, detail] = verdict(counts);
  console.log(`${state.padEnd(18)} ${detail}`);
  if (!FINDINGS.has(state)) return;

  for (const person of [...owners].sort((a, b) =>
    Number(a.added_at ?? 0) - Number(b.added_at ?? 0))) {
    const [, note] = unusedPrivilege(person, now, days);
    const extra = holders[String(person.id)] ? ' holds an admin API key' : '';
    const added = new Date(Number(person.added_at ?? 0) * 1000)
      .toISOString().slice(0, 10);
    console.log(`  ${mask(person.email).padEnd(24)} owner   added ${added}  ${note}${extra}`);
  }
  if (loose) {
    console.log(`  project roles: ${loose} of ${projects.length} project(s) also `
                + 'grant owner to every member');
  }
  const keyHolders = owners.filter((p) => holders[String(p.id)]).length;
  for (const line of repairLines(state, scim.length, keyHolders, loose)) {
    console.log(`  repair: ${line}`);
  }
  process.exitCode = 1;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
