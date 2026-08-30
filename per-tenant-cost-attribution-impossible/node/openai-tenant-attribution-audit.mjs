/**
 * Report whether OpenAI usage can be attributed to your customers at all.
 *
 * Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
 * organization admin key with read scopes.
 *
 * The finding is a fact about the reporting dimensions, not a number. user_id
 * on the Usage API is the org member or service account that owns the calling
 * key, never an end-user identifier you supplied. The repair is architectural,
 * forward-only, and printed rather than performed.
 */
const API = 'https://api.openai.com/v1';

// The complete list of dimensions the platform can attribute along.
const DIMENSIONS = ['user_id', 'api_key_id', 'project_id'];

const FINDINGS = ['single-key', 'keys-below-tenants'];

/**
 * Sum usage into the three dimensions the platform actually holds. Pure.
 * A null grouping value counts into the total but into no dimension, because
 * null means "not attributed" and inventing a bucket would flatter the result.
 */
export function fold(pages) {
  const out = { users: {}, keys: {}, projects: {}, requests: 0 };
  for (const page of pages) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const raw = Number(result.num_model_requests ?? 0);
        const n = Number.isFinite(raw) ? Math.trunc(raw) : 0;
        out.requests += n;
        for (const [field, key] of [['user_id', 'users'], ['api_key_id', 'keys'],
                                    ['project_id', 'projects']]) {
          const value = result[field];
          if (value) {
            const name = String(value);
            out[key][name] = (out[key][name] ?? 0) + n;
          }
        }
      }
    }
  }
  return out;
}

/**
 * What kind of principal is this user_id? Pure. "service-account", "member" or
 * "unresolved". The first two are the same finding in different clothes.
 */
export function classify(userId, directory) {
  const entry = directory[String(userId)];
  if (entry === undefined || entry === null) return 'unresolved';
  return entry.service_account ? 'service-account' : 'member';
}

/** user_ids generating usage that the org directory does not know. Pure. */
export function unresolved(folded, directory) {
  return Object.keys(folded.users ?? {})
    .filter((u) => classify(u, directory) === 'unresolved')
    .sort();
}

/**
 * Can this organization's usage be sliced per customer? Pure.
 * Returns [state, detail]. tenantCount comes from your database, because the
 * API has no concept of a tenant.
 */
export function verdict(folded, directory, tenantCount = null) {
  const keys = folded.keys ?? {};
  const users = folded.users ?? {};
  const total = folded.requests ?? 0;
  const keyCount = Object.keys(keys).length;
  const userCount = Object.keys(users).length;

  if (total <= 0 && keyCount === 0) {
    return ['no-usage',
      'no completions usage in the window, so there is nothing to attribute yet'];
  }

  const kinds = new Set(Object.keys(users).map((u) => classify(u, directory)));
  const principalNote = kinds.has('unresolved')
    ? `${userCount} user_id value(s), of which some resolve to nobody in the org directory`
    : `${userCount} user_id value(s), all of them org members or service accounts rather than customers`;

  if (keyCount === 1) {
    return ['single-key',
      '1 api_key_id covers every request in the window. There is one bucket, ' +
      `so per-customer cost has no place to come from. ${principalNote}.`];
  }

  if (tenantCount === null || tenantCount === undefined) {
    return ['unknown-tenant-count',
      `${keyCount} distinct api_key_id value(s) and ` +
      `${Object.keys(folded.projects ?? {}).length} project(s). ${principalNote}. ` +
      'Pass the tenant count to judge whether that is enough buckets.'];
  }

  if (keyCount < tenantCount) {
    return ['keys-below-tenants',
      `${keyCount} distinct api_key_id value(s) against ${tenantCount} ` +
      'tenant(s). Cost per customer is unrecoverable by construction: the ' +
      'finest slice the platform can offer is one key, and there are fewer ' +
      `keys than customers. ${principalNote}.`];
  }

  return ['segmented',
    `${keyCount} distinct api_key_id value(s) for ${tenantCount} tenant(s), so ` +
    'the platform can slice finely enough. Confirm your key-to-tenant map is current.'];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: OPENAI_ADMIN_KEY must be an organization ' +
                    'admin key, not a project key');
  }
  if (res.status === 403) {
    throw new Error('403 from OpenAI: the key is not authorised for /v1/organization');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function usagePages(key, startTime, days, maxPages = 20) {
  const pages = [];
  let params = {
    start_time: startTime, bucket_width: '1d', limit: days, group_by: DIMENSIONS,
  };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/usage/completions', params);
    pages.push(page);
    if (!page.next_page) break;
    params = { ...params, page: page.next_page };
  }
  return pages;
}

async function orgDirectory(key, maxPages = 20) {
  const out = {};
  let params = { limit: 100 };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/users', params);
    const data = page.data ?? [];
    for (const user of data) {
      out[String(user.id)] = {
        name: user.name ?? user.email ?? '?',
        service_account: Boolean(user.is_service_account),
      };
    }
    if (!page.has_more || data.length === 0) break;
    params = { limit: 100, after: data[data.length - 1].id };
  }
  return out;
}

async function spendByKey(key, startTime) {
  const out = {};
  const page = await get(key, '/organization/costs',
    { start_time: startTime, limit: 30, group_by: 'api_key_id' });
  for (const bucket of page.data ?? []) {
    for (const result of bucket.results ?? []) {
      const keyId = String(result.api_key_id ?? 'unattributed');
      const amount = Number(result.amount?.value ?? 0);
      if (Number.isFinite(amount)) out[keyId] = (out[keyId] ?? 0) + amount;
    }
  }
  return out;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read scopes)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const tenantsRaw = (process.env.TENANTS || "dummy-tenants");
  const tenants = tenantsRaw === undefined ? null : Number(tenantsRaw);

  const now = Math.floor(Date.now() / 1000);
  const folded = fold(await usagePages(key, now - days * 86400, days));
  const directory = await orgDirectory(key);
  const [state, detail] = verdict(folded, directory, tenants);

  console.log(`${state.padEnd(20)} ${detail}`);

  const byVolume = Object.keys(folded.users)
    .sort((a, b) => folded.users[b] - folded.users[a]);
  for (const userId of byVolume) {
    const kind = classify(userId, directory);
    const name = directory[userId]?.name ?? 'not in the directory';
    console.log(`  principal ${userId.padEnd(30)} ${kind.padEnd(16)} ${name}`);
  }

  const orphans = unresolved(folded, directory);
  if (orphans.length > 0) {
    console.warn(`  ${orphans.length} user_id value(s) resolve to nobody in the ` +
                 `org directory: ${orphans.join(', ')}`);
    console.warn('  repair: find what is calling as these principals before you ' +
                 'touch the attribution question');
  }

  if (FINDINGS.includes(state)) {
    const spend = await spendByKey(key, now - 30 * 86400);
    const top = Object.entries(spend).sort((a, b) => b[1] - a[1]).slice(0, 10);
    for (const [keyId, amount] of top) {
      console.warn(`  30d spend  ${keyId.padEnd(30)} $${amount.toFixed(2)}`);
    }
    console.warn('  repair: the Usage API cannot segment by end user. Mint one ' +
      'key, or one project, per tenant or tenant tier via ' +
      '/v1/organization/projects/{id}/service_accounts/{id}/api_keys and ' +
      'attribute with group_by=api_key_id.');
    console.warn('  repair: this is forward-only and cannot backfill. Until ' +
      'then, record each response usage block against your own tenant id and ' +
      'reconcile it against /v1/organization/costs.');
    process.exitCode = 1;
    return;
  }
  process.exitCode = 0;
}

// Only run when invoked directly, so importing this from the test file does not
// run main(), fail on the missing key, and set an exit code that fails the suite.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
