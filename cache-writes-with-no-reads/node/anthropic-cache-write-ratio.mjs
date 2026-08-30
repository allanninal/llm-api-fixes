/**
 * Report Anthropic cache writes that are never read back.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path, and an Admin key can be provisioned read-only.
 * The repair is printed, never performed.
 *
 * The usage report has no request-count field, so "reads per write" means read
 * tokens per write token: a proxy for call counts, not a call count.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// Published multipliers on base input.
const WRITE_5M = 1.25;
const WRITE_1H = 2.00;
const READ = 0.10;
const BASE = 1.00;

/** Sum token fields across results, keeping the two TTLs apart. Pure. */
export function accumulate(results, into = null) {
  const total = { uncached: 0, cache_read: 0, write_5m: 0, write_1h: 0, ...(into ?? {}) };
  for (const result of results ?? []) {
    total.uncached += Number(result.uncached_input_tokens ?? 0);
    total.cache_read += Number(result.cache_read_input_tokens ?? 0);
    const creation = result.cache_creation ?? {};
    total.write_5m += Number(creation.ephemeral_5m_input_tokens ?? 0);
    total.write_1h += Number(creation.ephemeral_1h_input_tokens ?? 0);
  }
  return total;
}

/**
 * Read tokens per write token at which caching starts to save money. Pure.
 * About 0.28 for pure 5m traffic, about 1.11 for pure 1h. Null when nothing
 * was written, because a ratio against zero is not a number.
 */
export function breakEvenRatio(write5m, write1h) {
  const writes = write5m + write1h;
  if (writes <= 0) return null;
  const premium = (WRITE_5M - BASE) * write5m + (WRITE_1H - BASE) * write1h;
  return premium / ((BASE - READ) * writes);
}

/**
 * What this cached traffic costs per token relative to not caching. Pure.
 * Above 1.0 means caching is charging a surcharge.
 */
export function effectiveMultiplier(write5m, write1h, reads) {
  const tokens = write5m + write1h + reads;
  if (tokens <= 0) return null;
  return (WRITE_5M * write5m + WRITE_1H * write1h + READ * reads) / tokens;
}

/** Classify one key's cache economics over the window. Pure. */
export function verdict(total, minWrites = 100_000, margin = 1.5) {
  const reads = Number(total.cache_read ?? 0);
  const write5m = Number(total.write_5m ?? 0);
  const write1h = Number(total.write_1h ?? 0);
  const writes = write5m + write1h;

  if (writes === 0 && reads === 0) {
    return ['no-caching',
      'no cache reads and no cache writes in this window: caching is not ' +
      'switched on for this key at all, which is a different problem from this one'];
  }
  if (writes === 0) {
    return ['reads-only',
      `${reads} read token(s) against entries written before this window opened. ` +
      'Widen the window before drawing a ratio from it.'];
  }
  if (writes < minWrites) {
    return ['too-little-traffic',
      `only ${writes} cache write token(s) in the window; too little to draw a ratio from`];
  }

  const ratio = reads / writes;
  const threshold = breakEvenRatio(write5m, write1h);
  const multiplier = effectiveMultiplier(write5m, write1h, reads);
  const shape = `${ratio.toFixed(2)} read tokens per write token against a ` +
    `break-even of ${threshold.toFixed(2)}; this traffic costs ` +
    `${multiplier.toFixed(2)}x what the same tokens would cost with caching switched off`;
  if (ratio < threshold) return ['losing', shape];
  if (ratio < threshold * margin) return ['marginal', `${shape}, which is barely above the line`];
  return ['paying-off', shape];
}

async function get(adminKey, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, one);
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': adminKey, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function* buckets(adminKey, path, params) {
  const q = { ...params };
  for (;;) {
    const page = await get(adminKey, path, q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q.page = page.next_page;
  }
}

/** Floor to the hour: starting_at must sit on a bucket boundary. */
export function windowStart(days, now = new Date()) {
  const top = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
                       now.getUTCHours());
  return new Date(top - days * 86400000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function main() {
  const adminKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!adminKey) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const minWrites = Number((process.env.MIN_WRITES || "dummy-min-writes") ?? 100_000);

  const params = {
    starting_at: windowStart(days),
    bucket_width: '1h',
    limit: Math.min(days * 24, 168),
    'group_by[]': ['api_key_id'],
  };

  const byKey = new Map();
  for await (const bucket of buckets(adminKey, '/organizations/usage_report/messages',
                                     params)) {
    for (const result of bucket.results ?? []) {
      const name = result.api_key_id ?? 'unattributed';
      byKey.set(name, accumulate([result], byKey.get(name)));
    }
  }

  if (byKey.size === 0) {
    console.log(`no message usage in the last ${days} day(s)`);
    return;
  }

  let losing = 0;
  const ordered = [...byKey.entries()].sort(
    (a, b) => (b[1].write_5m + b[1].write_1h) - (a[1].write_5m + a[1].write_1h));
  for (const [name, total] of ordered) {
    const [state, detail] = verdict(total, minWrites);
    const line = `${state.padEnd(18)} ${name}  ${detail}`;
    if (state !== 'losing' && state !== 'marginal') { console.log(line); continue; }
    losing += 1;
    console.warn(line);
    console.warn('  repair: move the cache_control breakpoint to the end of the stable ' +
                 "prefix and keep timestamps, request ids and the user's question " +
                 'strictly after it, then re-measure this ratio tomorrow');
    if (total.write_1h > total.write_5m) {
      console.warn('  note: most writes here are 1h entries at 2x base input, so ' +
                   'break-even needs about twice the reads a 5m entry does');
    }
    console.warn(`  confirm in money: GET ${API}/organizations/cost_report` +
                 '?starting_at=<T-30d>&group_by[]=description');
  }

  console.log(`${byKey.size} key(s), ${losing} losing money on caching`);
  process.exitCode = losing ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
