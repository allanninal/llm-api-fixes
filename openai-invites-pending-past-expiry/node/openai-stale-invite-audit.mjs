/**
 * Find OpenAI organization invites that lapsed without anybody noticing.
 *
 * Read only. Two paged GETs against /v1/organization/invites and
 * /v1/organization/users. No request body is constructed.
 *
 * The detection is a timestamp comparison rather than a status filter: an
 * invite can read status "pending" while its expires_at is already in the
 * past. Nothing secret is printed; the invite object carries no token.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;

const OWNER = 'owner';

const FINDINGS = new Set(['expired-but-still-pending', 'already-a-member',
                          'pending-stale', 'expired-uncollected']);

const SEVERITY = { 'expired-but-still-pending': 0, 'pending-stale': 1,
                   'expired-uncollected': 2, 'already-a-member': 3 };

/** When the invite was sent, as unix seconds. Pure. Both field names accepted. */
export function sentAt(invite) {
  for (const field of ['invited_at', 'created_at', 'sent_at']) {
    const value = invite?.[field];
    if (!value) continue;
    const n = Number(value);
    if (Number.isFinite(n)) return Math.trunc(n);
  }
  return null;
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

/** [[project_id, role]] carried by one invite. Pure. */
export function projectRoles(invite) {
  return (invite?.projects ?? []).map((entry) => [
    String(entry?.id ?? 'unknown'),
    String(entry?.role ?? 'member').trim().toLowerCase(),
  ]);
}

/** Does this invite hand over owner anywhere? Pure. Top level or per project. */
export function ownerGrant(invite) {
  if (String(invite?.role ?? '').trim().toLowerCase() === OWNER) return true;
  return projectRoles(invite).some(([, role]) => role === OWNER);
}

/** Lowercased email addresses on the current roster. Pure. */
export function memberEmails(users) {
  const out = new Set();
  for (const user of users ?? []) {
    const email = String(user?.email ?? '').trim().toLowerCase();
    if (email) out.add(email);
  }
  return out;
}

/** Classify one invite. Pure. expires_at is tested before status. */
export function classify(invite, members, now, staleDays = 14) {
  const row = invite ?? {};
  const status = String(row.status ?? '').trim().toLowerCase();
  const email = String(row.email ?? '').trim().toLowerCase();
  const sent = sentAt(row);
  const age = sent === null ? null : Math.floor((Number(now) - sent) / DAY);

  if (status === 'accepted') {
    return ['accepted', age === null ? 'accepted' : `accepted, sent ${age} day(s) ago`];
  }
  if (email && (members ?? new Set()).has(email)) {
    return ['already-a-member',
            'this address is already on the roster'
            + (age === null ? '' : `, invite sent ${age} day(s) ago`)];
  }
  if (status === 'expired') {
    return ['expired-uncollected',
            'expired and never cleaned up'
            + (age === null ? '' : `, sent ${age} day(s) ago`)];
  }
  if (status !== 'pending') {
    return ['unknown-status', `status "${status}" is not one this audit recognises`];
  }

  const expiresRaw = Number(row.expires_at ?? 0);
  const expires = Number.isFinite(expiresRaw) && expiresRaw > 0 ? expiresRaw : null;
  if (expires && expires < Number(now)) {
    return ['expired-but-still-pending',
            'still reads pending'
            + (age === null ? '' : ` ${age} day(s) after it was sent`)
            + `, and expires_at passed ${Math.floor((Number(now) - expires) / DAY)} `
            + 'day(s) ago. A filter on status alone never returns this row.'];
  }
  if (age !== null && age >= staleDays) {
    return ['pending-stale',
            `pending for ${age} day(s) and not yet past its expires_at`];
  }
  return ['pending', 'sent recently and still live'];
}

/** The repair for one classified invite. Pure. Printed, never performed. */
export function repairLines(state, invite) {
  const row = invite ?? {};
  const lines = [];
  if (!FINDINGS.has(state)) return lines;
  if (ownerGrant(row)) {
    lines.push('this invite still offers owner rights. Read it before you re-send '
      + 'anything: an uncollected owner grant only needs access to one mailbox.');
  }
  if (state === 'already-a-member') {
    lines.push('this person is already in the roster. Delete the record; there is '
      + 'no onboarding problem here.');
  } else if (state === 'expired-but-still-pending') {
    lines.push('the record is dead and still listed as pending. Delete it, then '
      + 'decide separately whether to re-send.');
  } else if (state === 'pending-stale') {
    lines.push('ask whether they ever received it. The API has no delivery status '
      + 'and cannot tell a filtered message from an ignored one.');
  } else {
    lines.push('expired and never cleaned up. Delete unless this person is still '
      + 'expected.');
  }
  const grants = projectRoles(row);
  if (grants.length && state !== 'already-a-member') {
    lines.push(`re-send with the same projects[] entries (${
      grants.map(([id, role]) => `${id}=${role}`).join(', ')}) or the new invite `
      + 'grants less than the first one did.');
  }
  lines.push(`DELETE /v1/organization/invites/${String(row.id ?? 'unknown')}`);
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
  const staleDays = Number((process.env.STALE_DAYS || "dummy-stale-days") ?? 14);
  const now = Math.floor(Date.now() / 1000);

  const members = memberEmails(await paged(admin, '/organization/users', { limit: 100 }));
  const invites = await paged(admin, '/organization/invites', { limit: 100 });

  const graded = invites.map((invite) => [invite, classify(invite, members, now, staleDays)]);
  const bad = graded.filter(([, [state]]) => FINDINGS.has(state));
  const accepted = graded.filter(([, [state]]) => state === 'accepted').length;

  console.log(`${invites.length} invite(s), ${accepted} accepted, ${bad.length} finding(s)`);

  bad.sort(([a, [sa]], [b, [sb]]) =>
    (ownerGrant(a) ? 0 : 1) - (ownerGrant(b) ? 0 : 1)
    || (SEVERITY[sa] ?? 9) - (SEVERITY[sb] ?? 9)
    || String(a.email ?? '').localeCompare(String(b.email ?? '')));

  for (const [invite, [state, detail]] of bad) {
    console.warn(`${state.padEnd(26)} ${mask(invite.email).padEnd(22)} `
                 + `role=${String(invite.role ?? '?').padEnd(7)} ${detail}`);
    const grants = projectRoles(invite);
    if (grants.length) {
      console.warn(`  grants: ${grants.map(([id, role]) => `${id}=${role}`).join(', ')}`);
    }
    for (const line of repairLines(state, invite)) console.warn(`  repair: ${line}`);
  }
  process.exitCode = bad.length ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
