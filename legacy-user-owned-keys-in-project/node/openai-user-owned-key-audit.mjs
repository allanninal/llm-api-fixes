/**
 * Find production keys whose owner is a person rather than a service account.
 *
 * Read only. Three GETs against the OpenAI Administration API with an admin
 * key. Nothing is created, changed or removed, and no key value is printed.
 *
 * The finding is the ownership type. The verdict function is never given an
 * organization total, so the share of the bill a key holds cannot influence
 * its grade: two personal keys splitting production spend evenly are two
 * findings here.
 *
 * Anthropic is not covered. Its key object has no owner-type distinction
 * between a person's credential and a service one, and no project
 * service-account object to compare against.
 */
const API = 'https://api.openai.com/v1';

export const USER = 'user';
export const SERVICE_ACCOUNT = 'service_account';
export const UNKNOWN = 'unknown';

export const IN_PRODUCTION = 'personal-key-in-production';
export const IDLE = 'personal-key-idle';
export const UNATTRIBUTABLE = 'unattributable-owner';
export const FINE = 'service-account-key';
const FINDINGS = new Set([IN_PRODUCTION, IDLE, UNATTRIBUTABLE]);

/** A key hint that is safe to print. Pure. Anything unredacted is withheld. */
export function safeHint(value) {
  const text = String(value ?? '').trim();
  if (!text) return '(no hint)';
  if ((!text.includes('...') && !text.includes('*')) || text.length > 40) {
    return '(hint withheld)';
  }
  return text;
}

/** Is this key owned by a person or a service account? Pure. Unknown stays unknown. */
export function ownerKind(key) {
  const owner = (key ?? {}).owner;
  if (!owner || typeof owner !== 'object') return UNKNOWN;
  const kind = String(owner.type ?? '').trim().toLowerCase();
  return (kind === USER || kind === SERVICE_ACCOUNT) ? kind : UNKNOWN;
}

/** A printable identity for the key's owner. Pure. Never a key value. */
export function ownerLabel(key) {
  const owner = (key ?? {}).owner;
  if (!owner || typeof owner !== 'object') return '(no owner block)';
  const kind = ownerKind(key);
  if (kind === USER) {
    const user = (owner.user && typeof owner.user === 'object') ? owner.user : {};
    return String(user.email ?? user.name ?? user.id ?? '(user, unnamed)');
  }
  if (kind === SERVICE_ACCOUNT) {
    const account = (owner.service_account && typeof owner.service_account === 'object')
      ? owner.service_account : {};
    return String(account.name ?? account.id ?? '(service account, unnamed)');
  }
  return `(owner type ${JSON.stringify(String(owner.type))})`;
}

/** Sum cost by api_key_id, keeping currency. Pure. */
export function foldCosts(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      for (const result of bucket?.results ?? []) {
        const keyId = result?.api_key_id;
        const amount = result?.amount;
        if (!keyId || !amount || typeof amount !== 'object') continue;
        const value = Number(amount.value ?? 0);
        if (!Number.isFinite(value)) continue;
        const currency = String(amount.currency ?? 'USD').toUpperCase();
        out[String(keyId)] = out[String(keyId)] ?? {};
        out[String(keyId)][currency] = (out[String(keyId)][currency] ?? 0) + value;
      }
    }
  }
  return out;
}

/** The largest single-currency amount recorded for one key. Pure. */
export function spendOf(costs, keyId) {
  const byCurrency = (costs ?? {})[String(keyId ?? '')] ?? {};
  const values = Object.values(byCurrency);
  return values.length ? Math.max(...values) : 0;
}

/** A printable spend summary for one key. Pure. Never adds currencies. */
export function spendLine(costs, keyId, days) {
  const byCurrency = (costs ?? {})[String(keyId ?? '')] ?? {};
  const entries = Object.entries(byCurrency).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return `no cost rows in ${days} day(s)`;
  const parts = entries.map(([currency, value]) => `${value.toFixed(2)} ${currency}`);
  return `${parts.join(' + ')} over ${days} day(s)`;
}

/** Classify one key by who owns it. Pure. No organization total is an input. */
export function verdict(key, keySpend, serviceAccountCount, minSpend = 1.0) {
  const kind = ownerKind(key);
  if (kind === SERVICE_ACCOUNT) return [FINE, 'owned by a service account'];
  if (kind === UNKNOWN) {
    return [UNATTRIBUTABLE,
      'the owner block is missing or its type is unrecognised, so nobody can ' +
      'say whose lifecycle this credential is attached to'];
  }
  if (Number(keySpend ?? 0) >= Number(minSpend)) {
    return [IN_PRODUCTION,
      'a person owns a credential carrying production spend' +
      (serviceAccountCount ? '' : ', in a project with no service accounts at all')];
  }
  return [IDLE,
    'owned by a person and carrying no measurable spend, so this is a ' +
    'revocation rather than a migration'];
}

/** The project-level finding, printed once per project. Pure. */
export function projectNote(projectName, userOwnedSpending, serviceAccountCount) {
  if (userOwnedSpending && !serviceAccountCount) {
    return `project ${projectName}: no service accounts at all, and ` +
           `${userOwnedSpending} user-owned key(s) are spending`;
  }
  return null;
}

/** The ordered cutover, printed and never performed. Pure. */
export function migrationPlan(projectId, keyId, keyName) {
  return [
    'create a service account for the service: an admin POST to ' +
    `/v1/organization/projects/${projectId}/service_accounts with a name ` +
    'that matches the deployable unit, not the person.',
    `mint its key under /v1/organization/projects/${projectId}/service_accounts/` +
    '{service_account_id}/api_keys. The value is returned exactly once, so ' +
    'capture it into the secret store in the same step.',
    'deploy the new value, then re-read the cost report grouped by ' +
    `api_key_id and confirm the spend has moved off ${keyName} (${keyId}).`,
    'only then revoke the old key with a DELETE on ' +
    `/v1/organization/projects/${projectId}/api_keys/${keyId}.`,
  ];
}

async function getJson(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function collect(key, path, params) {
  const rows = [];
  const pages = [];
  let after = null;
  for (;;) {
    const page = await getJson(key, path, after ? { ...params, after } : params);
    pages.push(page);
    rows.push(...(page.data ?? []));
    if (!page.has_more || !page.last_id) return { rows, pages };
    after = page.last_id;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an admin key (sk-admin-) with read ' +
                  'scopes; a project key cannot read /v1/organization/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minSpend = Number((process.env.MIN_SPEND || "dummy-min-spend") ?? 1.0);
  const start = Math.floor(Date.now() / 1000) - days * 86400;

  const costPages = await collect(admin, '/organization/costs', {
    start_time: start, limit: Math.min(days, 180), group_by: 'api_key_id' });
  const costs = foldCosts(costPages.pages);

  const projects = (await collect(admin, '/organization/projects',
                                  { limit: 100, include_archived: 'true' })).rows;

  let totalKeys = 0;
  let userOwned = 0;
  let findings = 0;
  let emptyRosters = 0;

  for (const project of projects) {
    if (!project.id) continue;
    const name = project.name ?? project.id;
    const keys = (await collect(admin, `/organization/projects/${project.id}/api_keys`,
                                { limit: 100, owner_project_access: 'any' })).rows;
    const accounts = (await collect(
      admin, `/organization/projects/${project.id}/service_accounts`, { limit: 100 })).rows;
    totalKeys += keys.length;

    const graded = keys.map((key) => {
      const keySpend = spendOf(costs, key.id);
      const [state, detail] = verdict(key, keySpend, accounts.length, minSpend);
      if (ownerKind(key) === USER) userOwned += 1;
      return { key, state, detail, keySpend };
    });

    const spending = graded.filter((row) => row.state === IN_PRODUCTION).length;
    const note = projectNote(name, spending, accounts.length);
    if (note) { emptyRosters += 1; console.warn(note); }

    for (const row of graded.sort((a, b) => b.keySpend - a.keySpend)) {
      if (!FINDINGS.has(row.state)) continue;
      findings += 1;
      console.warn(`${row.state.padEnd(27)} ${String(name).padEnd(12)} ` +
                   `${String(row.key.name ?? '(unnamed)').padEnd(12)} ` +
                   `${safeHint(row.key.redacted_value)}  ` +
                   `${ownerLabel(row.key).padEnd(24)} ` +
                   `${spendLine(costs, row.key.id, days)}`);
      console.warn(`  detail: ${row.detail}`);
      if (row.state === IN_PRODUCTION) {
        for (const step of migrationPlan(project.id, row.key.id,
                                         row.key.name ?? '(unnamed)')) {
          console.warn(`  repair: ${step}`);
        }
      } else if (row.state === IDLE) {
        console.warn('  repair: no traffic behind this one, so it is a ' +
                     'revocation rather than a migration.');
      }
    }
  }

  console.log(`${projects.length} project(s), ${totalKeys} key(s), ` +
              `${userOwned} owned by a user`);
  console.log(`${findings} finding(s), ${emptyRosters} project(s) with no service accounts`);
  console.log('share of the bill is not part of any verdict above: that is a ' +
              'different note and a different repair');
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
