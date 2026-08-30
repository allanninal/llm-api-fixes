/**
 * Report Claude traffic paying the US inference geo premium.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path.
 *
 * inference_geo "us" multiplies every token pricing category by 1.1 on Claude
 * 4.6 and later, and is usually inherited from a workspace's
 * data_residency.default_inference_geo rather than chosen per request. The
 * repair is printed, never applied: residency is a compliance setting.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// 1.1x on every token pricing category. Caching does not dilute it, because
// the cache rates are multiplied too.
const GEO_MULTIPLIER = 1.1;

// Every token field the multiplier touches. cache_creation is nested, and a
// flat read of it sums zero.
const FLAT_TOKEN_FIELDS = ['uncached_input_tokens', 'output_tokens',
                           'cache_read_input_tokens'];
const NESTED_TOKEN_FIELDS = ['ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'];

const FINDINGS = ['us-by-workspace-default', 'us-by-request', 'us-unexplained'];

/**
 * Normalise the inference_geo value. Pure. A null becomes "unspecified" and
 * never "global": one is traffic served globally, the other is traffic the
 * report declined to place.
 */
export function geoOf(result) {
  const raw = String(result?.inference_geo ?? '').trim().toLowerCase();
  return ['us', 'global', 'not_available'].includes(raw) ? raw : 'unspecified';
}

/** Sum every priced token category on one usage result. Pure. */
export function tokensOf(result) {
  let total = 0;
  for (const field of FLAT_TOKEN_FIELDS) {
    const n = Number(result?.[field] ?? 0);
    if (Number.isFinite(n)) total += Math.trunc(n);
  }
  const creation = result?.cache_creation;
  if (creation !== null && typeof creation === 'object' && !Array.isArray(creation)) {
    for (const field of NESTED_TOKEN_FIELDS) {
      const n = Number(creation[field] ?? 0);
      if (Number.isFinite(n)) total += Math.trunc(n);
    }
  }
  return total;
}

/** Sum priced tokens into {workspace_id: {geo: tokens}}. Pure. */
export function fold(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const workspace = String(result.workspace_id ?? 'default workspace');
        const geo = geoOf(result);
        if (!out[workspace]) out[workspace] = {};
        out[workspace][geo] = (out[workspace][geo] ?? 0) + tokensOf(result);
      }
    }
  }
  return out;
}

/** The share of priced tokens served on inference_geo us. Pure. */
export function usShare(geoTotals) {
  const values = Object.values(geoTotals ?? {}).map((v) => Number(v) || 0);
  const total = values.reduce((a, b) => a + b, 0);
  if (total <= 0) return 0;
  return (Number(geoTotals?.us) || 0) / total;
}

/**
 * Back the premium out of an amount that already contains it. Pure.
 *
 * NOT billed * share * 0.1. The billed figure is already 1.1x the base rate, so
 * the premium is (m - 1) / m of it, about 9.09%. Adding the multiplier on
 * instead of removing it overstates the saving by a tenth.
 */
export function premiumEstimate(billedDollars, share, multiplier = GEO_MULTIPLIER) {
  if (multiplier <= 1) return 0;
  const dollars = Math.max(0, Number(billedDollars ?? 0));
  const fraction = Math.min(1, Math.max(0, Number(share ?? 0)));
  return dollars * fraction * (multiplier - 1) / multiplier;
}

/**
 * A workspace's configured default inference geo. Pure. "unset" covers a
 * missing block and an unreadable one alike, because the repair is the same.
 */
export function residencyDefault(workspace) {
  const block = workspace?.data_residency;
  if (block === null || typeof block !== 'object' || Array.isArray(block)) return 'unset';
  const value = String(block.default_inference_geo ?? '').trim().toLowerCase();
  return ['us', 'global', 'not_available'].includes(value) ? value : 'unset';
}

/**
 * Classify one workspace. Pure. Returns [state, detail].
 * A workspace default and an explicit per-request parameter are kept apart:
 * the premium is identical, the owner of the fix is not.
 */
export function verdict(geoTotals, defaultGeo, minTokens = 1000000) {
  const totals = geoTotals ?? {};
  const total = Object.values(totals).reduce((a, b) => a + (Number(b) || 0), 0);
  if (total < minTokens) {
    return ['low-volume',
      `${total} priced token(s) in the window, too few to conclude anything`];
  }

  const us = Number(totals.us) || 0;
  if (us <= 0) {
    if ((Number(totals.not_available) || 0) >= total) {
      return ['geo-unsupported',
        `${(total / 1e6).toFixed(1)}M priced token(s), all on models that ` +
        'predate the inference_geo parameter. No premium and no lever.'];
    }
    return ['no-us-traffic',
      `${(total / 1e6).toFixed(1)}M priced token(s) and none of it on inference_geo us`];
  }

  const share = us / total;
  const shape = `${(share * 100).toFixed(0)}% of ${(total / 1e6).toFixed(1)}M ` +
                'priced token(s) on inference_geo us';

  if (defaultGeo === 'us') {
    return ['us-by-workspace-default',
      `${shape}; data_residency.default_inference_geo is us, so every caller ` +
      'pays the 1.1x whether or not any of them asked.'];
  }
  if (defaultGeo === 'global') {
    return ['us-by-request',
      `${shape} while the workspace default is global, so callers are setting ` +
      'inference_geo explicitly. The fix is in code, not in the workspace.'];
  }
  return ['us-unexplained',
    `${shape} with no readable data_residency default. Read the workspace ` +
    'before deciding whether this is deliberate.'];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function readPages(key, path, params) {
  const out = [];
  let next = { ...params };
  for (;;) {
    const page = await get(key, path, next);
    out.push(page);
    if (!page.has_more || !page.next_page) return out;
    next = { ...next, page: page.next_page };
  }
}

async function readWorkspaces(key) {
  const out = {};
  let params = { limit: 100, include_archived: 'true' };
  for (;;) {
    const page = await get(key, '/organizations/workspaces', params);
    for (const item of page.data ?? []) out[String(item.id)] = item;
    if (!page.has_more || !page.last_id) return out;
    params = { ...params, after_id: page.last_id };
  }
}

async function spendByWorkspace(key, start) {
  const out = {};
  for (const page of await readPages(key, '/organizations/cost_report',
    { starting_at: start, limit: 31, 'group_by[]': ['workspace_id'] })) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const workspace = String(result.workspace_id ?? 'default workspace');
        const value = Number(result.amount ?? 0);
        if (Number.isFinite(value)) out[workspace] = (out[workspace] ?? 0) + value;
      }
    }
  }
  return out;
}

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. */
function windowStart(days) {
  const midnight = new Date();
  midnight.setUTCHours(0, 0, 0, 0);
  midnight.setUTCDate(midnight.getUTCDate() - days);
  return midnight.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function main() {
  const key = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!key) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minTokens = Number((process.env.MIN_TOKENS || "dummy-min-tokens") ?? 1000000);
  const start = windowStart(days);

  const rows = fold(await readPages(key, '/organizations/usage_report/messages', {
    starting_at: start, bucket_width: '1d', limit: Math.min(days + 1, 31),
    'group_by[]': ['inference_geo', 'workspace_id'],
  }));
  const directory = await readWorkspaces(key);
  const spend = await spendByWorkspace(key, start);

  let checked = 0;
  let bad = 0;
  const ids = Object.keys(rows).sort((a, b) => (rows[b].us ?? 0) - (rows[a].us ?? 0));
  for (const workspace of ids) {
    const totals = rows[workspace];
    const defaultGeo = residencyDefault(directory[workspace]);
    const [state, detail] = verdict(totals, defaultGeo, minTokens);
    checked += 1;
    const line = `${state.padEnd(24)} ${workspace.padEnd(16)} ${detail}`;

    if (!FINDINGS.includes(state)) {
      console.log(line);
      continue;
    }
    bad += 1;
    console.warn(line);
    const billed = spend[workspace] ?? 0;
    console.warn(`  estimated premium about ` +
      `$${premiumEstimate(billed, usShare(totals)).toFixed(2)} of ` +
      `$${billed.toFixed(2)} spend in this window, assuming a similar token ` +
      'mix across geos');
    const allowed = directory[workspace]?.data_residency?.allowed_inference_geos;
    if (allowed) console.warn(`  allowed_inference_geos: ${allowed.join(', ')}`);
    if (state === 'us-by-workspace-default') {
      console.warn('  repair: confirm which contract requires US residency, and ' +
                   'whether that traffic can live in its own workspace instead ' +
                   'of every workspace paying for it');
    } else if (state === 'us-by-request') {
      console.warn('  repair: the callers are setting inference_geo themselves. ' +
                   'Find them before changing anything here.');
    } else {
      console.warn("  repair: read this workspace's data_residency block and " +
                   'record why it is set the way it is');
    }
    console.warn('  do not change residency from a script: it is a compliance ' +
                 'setting with a named owner');
  }

  console.log(`${checked} workspace(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
