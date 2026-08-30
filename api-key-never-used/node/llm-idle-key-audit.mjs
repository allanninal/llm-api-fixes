/**
 * Find API keys that no request has ever used.
 *
 * Read only. Every request is a GET against the OpenAI Administration API or
 * the Anthropic Admin API. No key value is printed: the providers return a
 * redacted hint and that hint is all that reaches the output.
 *
 * OpenAI carries last_used_at on the key object, so "never used" is a field.
 * Anthropic has no such field, so "unused" is a set difference against the
 * usage report and is bounded by that report's window. The Anthropic half of
 * the output says "unused in the last N days" and never says "never".
 */
const OPENAI = 'https://api.openai.com/v1';
const ANTHROPIC = 'https://api.anthropic.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

export const NEVER = 'never-used';
export const DORMANT = 'dormant';
export const UNUSED_IN_WINDOW = 'unused-in-window';
export const IN_USE = 'in-use';
export const SEEN = 'seen-in-window';
export const TOO_NEW = 'too-new';
export const UNREADABLE = 'unreadable-dates';
export const NOT_ACTIVE = 'not-active';

// Lower sorts first, and the order is revocation safety rather than age.
const SAFETY = { [NEVER]: 0, [UNUSED_IN_WINDOW]: 1, [DORMANT]: 2 };

/** A key hint that is safe to print. Pure. Anything unredacted is withheld. */
export function safeHint(value) {
  const text = String(value ?? '').trim();
  if (!text) return '(no hint)';
  if (!text.includes('...') && !text.includes('*')) return '(hint withheld)';
  if (text.length > 40) return '(hint withheld)';
  return text;
}

/** Whole days between a timestamp and now. Pure. null when unreadable.
 *  Accepts a unix integer (OpenAI) or an RFC 3339 string (Anthropic). */
export function ageDays(stamp, now) {
  if (stamp === null || stamp === undefined || stamp === '' ||
      typeof stamp === 'boolean') return null;
  let when;
  if (typeof stamp === 'number') {
    when = new Date(stamp * 1000);
  } else if (/^\d+$/.test(String(stamp).trim())) {
    when = new Date(Number(String(stamp).trim()) * 1000);
  } else {
    when = new Date(String(stamp).trim());
  }
  if (Number.isNaN(when.getTime())) return null;
  return Math.floor((now.getTime() - when.getTime()) / 86400000);
}

/** Classify one OpenAI key off last_used_at. Pure. Returns [state, detail]. */
export function openaiVerdict(key, now, neverAfter = 30, dormantAfter = 90) {
  const row = key ?? {};
  const created = ageDays(row.created_at, now);
  const last = row.last_used_at;
  if (last === null || last === undefined || last === '' || last === 0) {
    if (created === null) {
      return [UNREADABLE,
        'never used, and created_at cannot be read, so no age can be given for it'];
    }
    if (created < neverAfter) {
      return [TOO_NEW, `never used, but only ${created} day(s) old`];
    }
    return [NEVER, `never used in the ${created} day(s) since it was created`];
  }
  const idle = ageDays(last, now);
  if (idle === null) return [UNREADABLE, 'last_used_at is present but cannot be read'];
  if (idle >= dormantAfter) return [DORMANT, `last used ${idle} day(s) ago`];
  return [IN_USE, `last used ${idle} day(s) ago`];
}

/** Classify one Anthropic key off usage-report membership. Pure. */
export function anthropicVerdict(key, seenIds, windowDays, now, neverAfter = 30) {
  const row = key ?? {};
  const status = String(row.status ?? 'active').trim().toLowerCase();
  if (status !== 'active') {
    return [NOT_ACTIVE, `status is ${status}, so it cannot authenticate`];
  }
  const created = ageDays(row.created_at, now);
  if (created !== null && created < neverAfter) {
    return [TOO_NEW, `only ${created} day(s) old`];
  }
  if ((seenIds ?? new Set()).has(String(row.id ?? ''))) {
    return [SEEN, `carried traffic inside the last ${windowDays} day(s)`];
  }
  return [UNUSED_IN_WINDOW,
    `no traffic in the last ${windowDays} day(s). The Anthropic key object ` +
    'has no last_used_at field, so this is unused within the retrievable ' +
    'window and not a claim that it was never used.'];
}

/** Warn about a sweep that will silently under-report. Pure. */
export function auditGaps(projectParams, keyParams) {
  const gaps = [];
  if (String((projectParams ?? {}).include_archived ?? '').toLowerCase() !== 'true') {
    gaps.push('include_archived is not true: archived projects are omitted ' +
              'from the project listing, and every key inside them with it');
  }
  if (String((keyParams ?? {}).owner_project_access ?? '') !== 'any') {
    gaps.push("owner_project_access is not 'any': the key listing applies " +
              'membership visibility rules and can hide enabled keys from ' +
              'this audit');
  }
  return gaps;
}

/** Every non-null api_key_id in an Anthropic usage report. Pure. */
export function seenKeyIds(pages) {
  const out = new Set();
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      for (const result of bucket?.results ?? []) {
        if (result?.api_key_id) out.add(String(result.api_key_id));
      }
    }
  }
  return out;
}

/** Order findings by how safe each is to revoke. Pure. */
export function revocationOrder(rows) {
  return (rows ?? [])
    .filter((r) => r?.state in SAFETY)
    .slice()
    .sort((a, b) => (SAFETY[a.state] - SAFETY[b.state])
      || (Number(b.idle ?? 0) - Number(a.idle ?? 0))
      || String(a.name ?? '').localeCompare(String(b.name ?? '')));
}

/** The repair for one classified key. Pure. Printed, never performed. */
export function repairLines(state, row) {
  const data = row ?? {};
  if (state === NEVER) {
    return [
      'nothing has ever authenticated with this key, so revoking it cannot ' +
      'break traffic. These are the safest credentials in the organization to remove.',
      `revoke with a DELETE on /v1/organization/projects/${data.container ?? '{project_id}'}` +
      `/api_keys/${data.id ?? '{key_id}'} once somebody confirms what it was minted for.`,
    ];
  }
  if (state === DORMANT) {
    return [
      'something was built on this key and has since stopped calling. Ask ' +
      'what it was before revoking: annual jobs and disaster-recovery paths ' +
      'look exactly like this.',
      'if it is genuinely dead, revoke it and confirm last_used_at stops ' +
      'advancing rather than assuming it will.',
    ];
  }
  if (state === UNUSED_IN_WINDOW) {
    return [
      'this is unused within the report window, not proven unused. Widen the ' +
      'window as far as the report allows before concluding anything, then ' +
      'archive the key rather than deleting it.',
      'the Anthropic key object carries an optional expires_at. Set one on ' +
      'the replacement so the next idle key expires itself.',
    ];
  }
  return [];
}

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. */
export function windowStart(days, now = new Date()) {
  const midnight = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                     now.getUTCDate()));
  midnight.setUTCDate(midnight.getUTCDate() - days);
  return `${midnight.toISOString().slice(0, 19)}Z`;
}

async function getJson(url, headers, who) {
  const res = await fetch(url, { headers });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from ${who}: this endpoint needs an ` +
                    'administration key, not a project or workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function openaiPaged(key, path, params) {
  const headers = { Authorization: `Bearer ${key}` };
  const out = [];
  let after = null;
  for (;;) {
    const url = new URL(OPENAI + path);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
    if (after) url.searchParams.set('after', after);
    const page = await getJson(url, headers, 'OpenAI');
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.last_id) return out;
    after = page.last_id;
  }
}

async function anthropicPaged(key, path, params) {
  const headers = { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION };
  const out = [];
  let afterId = null;
  for (;;) {
    const url = new URL(ANTHROPIC + path);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
    if (afterId) url.searchParams.set('after_id', afterId);
    const page = await getJson(url, headers, 'Anthropic');
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.last_id) return out;
    afterId = page.last_id;
  }
}

async function anthropicReport(key, params) {
  const headers = { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION };
  const pages = [];
  let page = null;
  for (;;) {
    const url = new URL(`${ANTHROPIC}/organizations/usage_report/messages`);
    for (const [k, v] of params) url.searchParams.append(k, v);
    if (page) url.searchParams.set('page', page);
    const body = await getJson(url, headers, 'Anthropic');
    pages.push(body);
    if (!body.has_more || !body.next_page) return pages;
    page = body.next_page;
  }
}

async function main() {
  const openaiKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  const anthropicKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!openaiKey && !anthropicKey) {
    console.error('set OPENAI_ADMIN_KEY (sk-admin-, read scopes) or ' +
                  'ANTHROPIC_ADMIN_KEY (sk-ant-admin), or both; a project or ' +
                  'workspace key cannot read the administration endpoints');
    process.exitCode = 2;
    return;
  }
  const neverAfter = Number((process.env.NEVER_AFTER || "dummy-never-after") ?? 30);
  const dormantAfter = Number((process.env.DORMANT_AFTER || "dummy-dormant-after") ?? 90);
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const now = new Date();
  const rows = [];

  if (openaiKey) {
    const projectParams = { limit: 100, include_archived: 'true' };
    const keyParams = { limit: 100, owner_project_access: 'any' };
    for (const gap of auditGaps(projectParams, keyParams)) {
      console.warn(`audit gap: ${gap}`);
    }
    const projects = await openaiPaged(openaiKey, '/organization/projects', projectParams);
    for (const project of projects) {
      if (!project.id) continue;
      const keys = await openaiPaged(
        openaiKey, `/organization/projects/${project.id}/api_keys`, keyParams);
      for (const key of keys) {
        const [state, detail] = openaiVerdict(key, now, neverAfter, dormantAfter);
        rows.push({ state, detail, id: key.id, name: key.name ?? '(unnamed)',
                    hint: safeHint(key.redacted_value), container: project.id,
                    label: project.name ?? project.id,
                    idle: ageDays(key.last_used_at, now) ?? ageDays(key.created_at, now) ?? 0 });
      }
    }
    const adminKeys = await openaiPaged(openaiKey, '/organization/admin_api_keys', { limit: 100 });
    for (const key of adminKeys) {
      const [state, detail] = openaiVerdict(key, now, neverAfter, dormantAfter);
      rows.push({ state, detail, id: key.id, name: key.name ?? '(unnamed)',
                  hint: safeHint(key.redacted_value), container: 'organization',
                  label: 'admin key',
                  idle: ageDays(key.last_used_at, now) ?? ageDays(key.created_at, now) ?? 0 });
    }
    console.log(`openai: ${projects.length} project(s) read, ${rows.length} key(s) ` +
                `including ${adminKeys.length} admin key(s)`);
  }

  if (anthropicKey) {
    const keys = await anthropicPaged(anthropicKey, '/organizations/api_keys',
                                      { status: 'active', limit: 1000 });
    const seen = seenKeyIds(await anthropicReport(anthropicKey, [
      ['starting_at', windowStart(days, now)], ['bucket_width', '1d'],
      ['limit', String(Math.min(days + 1, 31))], ['group_by[]', 'api_key_id'],
    ]));
    for (const key of keys) {
      const [state, detail] = anthropicVerdict(key, seen, days, now, neverAfter);
      rows.push({ state, detail, id: key.id, name: key.name ?? '(unnamed)',
                  hint: safeHint(key.partial_key_hint), container: key.id,
                  label: 'anthropic', idle: ageDays(key.created_at, now) ?? 0 });
    }
    console.log(`anthropic: ${keys.length} active key(s), ${seen.size} seen in ` +
                `the usage report over ${days} day(s)`);
  }

  const queue = revocationOrder(rows);
  for (const row of queue) {
    console.warn(`${row.state.padEnd(16)} ${String(row.label).padEnd(14)} ` +
                 `${String(row.name).padEnd(18)} ${row.hint}  ${row.detail}`);
    for (const repair of repairLines(row.state, row)) {
      console.warn(`  repair: ${repair}`);
    }
  }
  console.log(`${rows.length} key(s) read, ${queue.length} finding(s)`);
  console.log('no key value appears above: both providers return a redacted ' +
              'hint and the hint is all this script will print');
  process.exitCode = queue.length ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
