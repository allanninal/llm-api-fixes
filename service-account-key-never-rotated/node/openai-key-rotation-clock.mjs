/**
 * Find service account keys that have never been rotated.
 *
 * Read only. Four GETs against the OpenAI Administration API with an admin
 * key. Nothing is created, changed or removed, and no key value is printed.
 *
 * The clock is created_at on the newest key belonging to each service account,
 * because no rotated_at field exists on either provider. The key count decides
 * which of three findings it is.
 *
 * The audit log corroborates an absence at the PROJECT level only: the
 * api_key.created event carries project.id and actor and does not name a
 * service account. An empty or unreachable log is reported as unavailable.
 */
const API = 'https://api.openai.com/v1';

export const SINGLE_STALE = 'single-stale-key';
export const STALE = 'stale-key';
export const UNFINISHED = 'unfinished-rotation';
export const NO_KEYS = 'service-account-with-no-keys';
export const ROTATING = 'rotating';
export const TOO_NEW = 'too-new';
const FINDINGS = new Set([SINGLE_STALE, STALE, UNFINISHED, NO_KEYS]);

export const AUDIT_CONFIRMED = 'confirmed-at-project-level';
export const AUDIT_ACTIVITY = 'creation-activity-in-window';
export const AUDIT_UNAVAILABLE = 'audit-unavailable';

/** Whole days between a unix timestamp and now. Pure. null when unreadable. */
export function ageDays(stamp, now) {
  if (stamp === null || stamp === undefined || stamp === '' ||
      typeof stamp === 'boolean') return null;
  const seconds = Number(stamp);
  if (!Number.isFinite(seconds)) return null;
  return Math.floor((now.getTime() - seconds * 1000) / 86400000);
}

/** The service account a key belongs to, or null. Pure. */
export function serviceAccountId(key) {
  const owner = (key ?? {}).owner;
  if (!owner || typeof owner !== 'object') return null;
  if (String(owner.type ?? '').trim().toLowerCase() !== 'service_account') return null;
  const account = owner.service_account;
  if (!account || typeof account !== 'object') return null;
  return String(account.id ?? '') || null;
}

/** Group service-account keys by owner.service_account.id. Pure. */
export function groupByAccount(keys) {
  const out = {};
  for (const key of keys ?? []) {
    const account = serviceAccountId(key);
    if (!account) continue;
    out[account] = out[account] ?? [];
    out[account].push(key);
  }
  return out;
}

/** [newestAge, oldestAge] in days across a key group. Pure. */
export function newestAndOldest(keys, now) {
  const ages = (keys ?? [])
    .map((k) => ageDays((k ?? {}).created_at, now))
    .filter((a) => a !== null);
  if (!ages.length) return [null, null];
  return [Math.min(...ages), Math.max(...ages)];
}

/** Classify one service account's rotation state. Pure. [state, detail]. */
export function rotationVerdict(account, keys, now, staleAfter = 180, minAge = 30) {
  const name = String((account ?? {}).name ?? (account ?? {}).id ?? '(unnamed)');
  const rows = [...(keys ?? [])];
  if (!rows.length) {
    const created = ageDays((account ?? {}).created_at, now);
    return [NO_KEYS,
      `service account ${name} has no keys at all` +
      (created === null ? '' : `, and was created ${created} day(s) ago`)];
  }
  const [newest, oldest] = newestAndOldest(rows, now);
  if (newest === null) {
    return [TOO_NEW, `no readable created_at on any of its ${rows.length} key(s)`];
  }
  if (newest < minAge && rows.length === 1) {
    return [TOO_NEW, `its only key is ${newest} day(s) old`];
  }
  if (newest < staleAfter) {
    if (oldest >= staleAfter && rows.length > 1) {
      return [UNFINISHED,
        `newest key ${newest} day(s) old, oldest ${oldest} day(s) and still live`];
    }
    return [ROTATING, `newest key ${newest} day(s) old`];
  }
  if (rows.length === 1) {
    return [SINGLE_STALE, `newest key ${newest} day(s) old, and it is the only one`];
  }
  return [STALE, `newest key ${newest} day(s) old across ${rows.length} key(s)`];
}

/** What the audit log can and cannot confirm. Pure. [state, detail]. */
export function corroboration(events, projectId, auditReachable = true, days = 180) {
  if (!auditReachable) {
    return [AUDIT_UNAVAILABLE,
      'the audit log could not be read, so nothing here is corroborated. ' +
      'Audit logging is gated to organizations that have it enabled and its ' +
      'silence is not evidence.'];
  }
  const rows = [...(events ?? [])];
  if (!rows.length) {
    return [AUDIT_UNAVAILABLE,
      `the audit log returned no events of any kind in ${days} day(s), which ` +
      'can mean nothing was minted or can mean nothing is being recorded. ' +
      'Treated as unavailable rather than clean.'];
  }
  const here = rows.filter(
    (e) => String((e?.project ?? {}).id ?? '') === String(projectId));
  if (here.length) {
    return [AUDIT_ACTIVITY,
      `${here.length} api_key.created event(s) in this project in ${days} ` +
      'day(s), so something was minted here. The event does not name a ' +
      'service account, so it neither confirms nor clears any one of them.'];
  }
  return [AUDIT_CONFIRMED,
    `no api_key.created events in this project in ${days} day(s). That is a ` +
    'project-level fact: the event carries project.id and actor and not the ' +
    'service account, so the per-account age above remains the evidence for ' +
    'any single account.'];
}

/** The overlap rotation, printed and never performed. Pure. */
export function rotationPlan(projectId, accountName, singleKey) {
  const steps = [];
  if (singleKey) {
    steps.push('mint a second key first. One key means every rotation is a ' +
               'hard cutover with no rollback, which is the actual reason ' +
               'this has not happened yet.');
  }
  steps.push(
    'mint the replacement with an admin POST to /v1/organization/projects/' +
    `${projectId}/service_accounts/{service_account_id}/api_keys for ` +
    `${accountName}. The value is returned exactly once.`,
    'deploy the new value everywhere the old one is held, then watch the old ' +
    'key: its last_used_at should stop advancing within one traffic cycle. ' +
    'Do not skip this; it is the only rollback you get.',
    `revoke the old key with a DELETE on /v1/organization/projects/${projectId}` +
    '/api_keys/{api_key_id}, and diary the next rotation at 90 days. Project ' +
    'keys have no expires_at, so nothing will remind you.');
  return steps;
}

async function getJson(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of params) url.searchParams.append(k, String(v));
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'admin key (sk-admin-), not a project key');
  }
  if (!res.ok) {
    const err = new Error(`${res.status} from ${path}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function collect(key, path, params) {
  const rows = [];
  let after = null;
  for (let page = 0; page < 20; page += 1) {
    const query = after ? [...params, ['after', after]] : params;
    const body = await getJson(key, path, query);
    rows.push(...(body.data ?? []));
    if (!body.has_more || !body.last_id) return rows;
    after = body.last_id;
  }
  return rows;
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an admin key (sk-admin-) with read ' +
                  'scopes; a project key cannot read /v1/organization/*');
    process.exitCode = 2;
    return;
  }
  const staleAfter = Number((process.env.STALE_AFTER || "dummy-stale-after") ?? 180);
  const minAge = Number((process.env.MIN_AGE || "dummy-min-age") ?? 30);
  const now = new Date();
  const since = Math.floor(now.getTime() / 1000) - staleAfter * 86400;

  let events = [];
  let reachable = true;
  try {
    events = await collect(admin, '/organization/audit_logs', [
      ['limit', 100], ['event_types[]', 'api_key.created'],
      ['effective_at[gte]', since]]);
    console.log(`audit log: ${events.length} api_key.created event(s) in ` +
                `${staleAfter} day(s)`);
  } catch (err) {
    reachable = false;
    console.warn(`audit log unreadable (${err.status ?? err.message}): ` +
                 'rotation ages below stand on created_at alone');
  }

  const projects = await collect(admin, '/organization/projects',
                                 [['limit', 100], ['include_archived', 'true']]);
  let accountsSeen = 0;
  let keysSeen = 0;
  let findings = 0;

  for (const project of projects) {
    if (!project.id) continue;
    const name = project.name ?? project.id;
    const accounts = await collect(
      admin, `/organization/projects/${project.id}/service_accounts`, [['limit', 100]]);
    const keys = await collect(
      admin, `/organization/projects/${project.id}/api_keys`,
      [['limit', 100], ['owner_project_access', 'any']]);
    const grouped = groupByAccount(keys);
    accountsSeen += accounts.length;
    keysSeen += Object.values(grouped).reduce((n, v) => n + v.length, 0);

    let projectFindings = 0;
    for (const account of accounts) {
      const rows = grouped[String(account.id ?? '')] ?? [];
      const [state, detail] = rotationVerdict(account, rows, now, staleAfter, minAge);
      if (!FINDINGS.has(state)) continue;
      findings += 1;
      projectFindings += 1;
      console.warn(`${state.padEnd(19)} ${String(name).padEnd(11)} ` +
                   `${String(account.name ?? account.id ?? '(unnamed)').padEnd(15)} ${detail}`);
      if (state === SINGLE_STALE || state === STALE || state === UNFINISHED) {
        for (const step of rotationPlan(project.id, account.name ?? '(unnamed)',
                                        state === SINGLE_STALE)) {
          console.warn(`  repair: ${step}`);
        }
      }
    }

    if (projectFindings) {
      const [auditState, auditDetail] =
        corroboration(events, project.id, reachable, staleAfter);
      console.log(`corroboration for ${name}: ${auditState}: ${auditDetail}`);
    }
  }

  console.log(`${projects.length} project(s), ${accountsSeen} service account(s), ` +
              `${keysSeen} service-account key(s)`);
  console.log(`${findings} finding(s)`);
  console.log('there is no rotated_at field on either provider: created_at on ' +
              'the newest key is the only clock available');
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
