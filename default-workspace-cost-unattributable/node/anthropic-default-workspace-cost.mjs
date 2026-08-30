/**
 * Find Anthropic cost that reports no workspace, and the keys behind it.
 *
 * Read only. Three paged GETs against /v1/organizations/* with an Admin API
 * key. No request body is constructed and no key value is read or printed.
 *
 * The unallocated bucket has two causes and only one has a repair: keys that
 * land in the default workspace can be moved, and Console playground traffic
 * carries no key at all.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const DEFAULT_WS = '(default workspace)';

const PLAYGROUND = 'console-playground';
const DEFAULT_KEYED = 'default-workspace';
const ATTRIBUTED = 'attributed';

const ORG_SCOPED = 'organization-scoped';
const NAMED = 'named-workspace';
const UNKNOWN_SCOPE = 'unknown-scope';

const MOVABLE = [ORG_SCOPED, DEFAULT_KEYED];
const FINDINGS = new Set(['movable-keys', 'console-playground',
                          'unattributable-no-key-to-move']);

const money = (n) => Number(n).toLocaleString('en-US',
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** One cost row's amount as a number. Pure. amount is a decimal STRING. */
export function amount(row) {
  const value = Number(row?.amount ?? 0);
  return Number.isFinite(value) ? value : 0;
}

/** {workspace_id: dollars} from the cost report. Pure. Null uses a sentinel. */
export function costByWorkspace(pages) {
  const rows = {};
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      for (const result of bucket?.results ?? []) {
        const key = String(result?.workspace_id ?? DEFAULT_WS);
        rows[key] = (rows[key] ?? 0) + amount(result);
      }
    }
  }
  return rows;
}

/** The null workspace's share of total cost. Pure. */
export function unattributedShare(rows) {
  const data = rows ?? {};
  const total = Object.values(data).reduce((a, v) => a + v, 0);
  if (total <= 0) return 0;
  return (data[DEFAULT_WS] ?? 0) / total;
}

/** Total billed tokens on one usage row. Pure. cache_creation is an object. */
export function weigh(result) {
  const row = result ?? {};
  let total = 0;
  for (const field of ['uncached_input_tokens', 'cache_read_input_tokens',
                       'output_tokens']) {
    const value = Number(row[field] ?? 0);
    if (Number.isFinite(value)) total += value;
  }
  const creation = row.cache_creation;
  if (creation && typeof creation === 'object' && !Array.isArray(creation)) {
    for (const value of Object.values(creation)) {
      const n = Number(value ?? 0);
      if (Number.isFinite(n)) total += n;
    }
  }
  return total;
}

/** {cause: tokens}. Pure. A null api_key_id is classified before a null workspace. */
export function usageSplit(pages) {
  const out = { [PLAYGROUND]: 0, [DEFAULT_KEYED]: 0, [ATTRIBUTED]: 0 };
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      for (const result of bucket?.results ?? []) {
        const tokens = weigh(result);
        if (!result?.api_key_id) out[PLAYGROUND] += tokens;
        else if (!result?.workspace_id) out[DEFAULT_KEYED] += tokens;
        else out[ATTRIBUTED] += tokens;
      }
    }
  }
  return out;
}

/** Playground share of the null bucket only. Pure. */
export function playgroundShare(split) {
  const data = split ?? {};
  const bucket = (data[PLAYGROUND] ?? 0) + (data[DEFAULT_KEYED] ?? 0);
  if (bucket <= 0) return 0;
  return (data[PLAYGROUND] ?? 0) / bucket;
}

/** Where one API key's traffic lands. Pure. Returns [kind, workspaceId]. */
export function keyAttribution(key) {
  const row = key ?? {};
  const scope = row.scope ?? {};
  const kind = String(scope.type ?? '').trim().toLowerCase();
  const workspace = scope.workspace_id ?? row.workspace_id ?? null;

  if (kind === 'organization') return [ORG_SCOPED, null];
  if (kind && kind !== 'workspace') {
    return [UNKNOWN_SCOPE, workspace ? String(workspace) : null];
  }
  if (workspace) return [NAMED, String(workspace)];
  return [DEFAULT_KEYED, null];
}

/** {kind: [{id, name, workspace_id}]} over ACTIVE keys only. Pure. */
export function foldKeys(keys) {
  const out = { [ORG_SCOPED]: [], [DEFAULT_KEYED]: [], [NAMED]: [],
                [UNKNOWN_SCOPE]: [] };
  for (const key of keys ?? []) {
    const row = key ?? {};
    if (String(row.status ?? 'active').trim().toLowerCase() !== 'active') continue;
    const [kind, workspace] = keyAttribution(row);
    out[kind].push({ id: String(row.id ?? 'unknown'),
                     name: String(row.name ?? 'unnamed'),
                     workspace_id: workspace });
  }
  return out;
}

/** Classify the unallocated bucket. Pure. Returns [state, detail]. */
export function verdict(share, total, folded, split, minSpend = 1.0,
                        minShare = 0.10, playgroundMax = 0.50) {
  const movable = MOVABLE.reduce((a, kind) => a + (folded?.[kind]?.length ?? 0), 0);
  if (total < minSpend) {
    return ['no-spend-yet',
            `$${money(total)} of cost in the window, too little to conclude anything`];
  }
  if (share < minShare) {
    return ['attributed',
            `${(share * 100).toFixed(0)}% of $${money(total)} has a null `
            + 'workspace_id, under the threshold'];
  }
  const plays = playgroundShare(split);
  if (plays > playgroundMax) {
    return ['console-playground',
            `${(share * 100).toFixed(0)}% of $${money(total)} has no workspace on it, `
            + `and ${(plays * 100).toFixed(0)}% of that usage carries no api_key_id `
            + 'either. That is Console playground traffic, and no key can be moved '
            + 'to make it land anywhere.'];
  }
  if (movable) {
    return ['movable-keys',
            `${(share * 100).toFixed(0)}% of $${money(total)} has no workspace on it, `
            + `and ${movable} active key(s) land in the default workspace or carry `
            + 'organization scope.'];
  }
  return ['unattributable-no-key-to-move',
          `${(share * 100).toFixed(0)}% of $${money(total)} has no workspace on it, `
          + 'and every active key resolves to a named workspace. The spend came from '
          + 'a key that has since been deleted, or from the playground.'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, folded, split) {
  const plays = playgroundShare(split);
  if (state === 'movable-keys') {
    const lines = ['recreate each key inside a named workspace and cut over, key by '
                   + "key. A key's workspace is fixed when it is created."];
    if (folded?.[ORG_SCOPED]?.length) {
      lines.push(`${folded[ORG_SCOPED].length} of them carry organization scope, `
        + 'which is not a workspace at all: those cannot be reassigned, only replaced.');
    }
    if (plays > 0) {
      lines.push(`${(plays * 100).toFixed(0)}% of the null usage is Console `
        + 'playground and no key move touches it.');
    }
    lines.push('the default workspace cannot carry a rate-limit override at all, so '
      + 'this traffic is also unbounded relative to the organization limit.');
    return lines;
  }
  if (state === 'console-playground') {
    return [
      'there is no key migration here. The requests carried no key.',
      'decide where experiments should run: a named workspace with its own key, or '
      + 'an accepted line in the chargeback report.',
      'the default workspace cannot carry a rate-limit override, so playground '
      + 'traffic competes with production for the org limit.',
    ];
  }
  if (state === 'unattributable-no-key-to-move') {
    return [
      'do not open a migration ticket. Every active key already resolves to a '
      + 'named workspace.',
      'the spend predates a key deletion or came from the playground; narrow the '
      + 'window and read the daily buckets to see which.',
    ];
  }
  return [];
}

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. Pure given now. */
export function windowStart(days, now = new Date()) {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return `${new Date(midnight - days * 86400000).toISOString().slice(0, 19)}Z`;
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const one of v) url.searchParams.append(k, String(one));
    else url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from Anthropic: /v1/organizations/* needs an Admin `
                    + 'API key (sk-ant-admin...), not a workspace key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function reportPages(key, path, params) {
  const out = [];
  const q = { ...params };
  for (;;) {
    const page = await read(key, path, q);
    out.push(page);
    if (!page.has_more || !page.next_page) return out;
    q.page = page.next_page;
  }
}

async function listing(key, path, params) {
  const out = [];
  const q = { ...params };
  for (;;) {
    const page = await read(key, path, q);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.last_id) return out;
    q.after_id = page.last_id;
  }
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); '
                  + 'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const start = windowStart(days);
  const limit = Math.min(days + 1, 31);

  const costs = costByWorkspace(await reportPages(admin, '/organizations/cost_report',
    { starting_at: start, limit, 'group_by[]': ['workspace_id'] }));
  const total = Math.round(Object.values(costs).reduce((a, v) => a + v, 0) * 100) / 100;
  const share = unattributedShare(costs);

  const split = usageSplit(await reportPages(admin,
    '/organizations/usage_report/messages',
    { starting_at: start, bucket_width: '1d', limit,
      'group_by[]': ['api_key_id', 'workspace_id'] }));

  const folded = foldKeys(await listing(admin, '/organizations/api_keys', { limit: 100 }));

  console.log(`$${money(total)} in the last ${days} day(s) across `
              + `${Object.keys(costs).length} workspace row(s)`);
  console.log(`unattributed: $${money(costs[DEFAULT_WS] ?? 0)} `
              + `(${(share * 100).toFixed(0)}% of spend) has a null workspace_id`);
  const plays = playgroundShare(split);
  console.log(`usage split of the null bucket: ${((1 - plays) * 100).toFixed(0)}% from `
              + `API keys, ${(plays * 100).toFixed(0)}% Console playground`);

  const [state, detail] = verdict(share, total, folded, split);
  console.log(`${state.padEnd(18)} ${detail}`);
  if (FINDINGS.has(state)) {
    for (const kind of MOVABLE) {
      for (const key of folded[kind] ?? []) {
        console.log(`  ${key.id.padEnd(12)} ${key.name.padEnd(22)} ${kind}`);
      }
    }
    for (const line of repairLines(state, folded, split)) {
      console.log(`  repair: ${line}`);
    }
  }
  process.exitCode = FINDINGS.has(state) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
