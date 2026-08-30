/**
 * Report an Anthropic organization that never switched prompt caching on.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path, and an Admin key can be provisioned read-only.
 * The repair is printed, never performed.
 *
 * The messages usage report carries token sums and no request count, so every
 * ratio here is a ratio of tokens, never of calls.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';
const READ_MULTIPLIER = 0.10;

/**
 * Sum the token fields that matter across usage-report results. Pure.
 * cache_creation is a nested object; reading it flat sums zero and reports a
 * heavily cached organization as an uncached one.
 */
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
 * Base-rate tokens you could stop paying for, at best. Pure and deliberately a
 * ceiling: nothing in the API can tell you what fraction of your input is
 * really a stable prefix, because the API never returns your prompts.
 */
export function cacheSavingCeiling(uncachedTokens, reusableFraction) {
  if (!(reusableFraction >= 0 && reusableFraction <= 1)) {
    throw new RangeError('reusableFraction must be between 0 and 1');
  }
  return Math.floor(Math.max(0, uncachedTokens) * reusableFraction * (1 - READ_MULTIPLIER));
}

/** Classify one workload's token totals. Pure. */
export function verdict(total, minInput = 1_000_000) {
  const reads = Number(total.cache_read ?? 0);
  const writes = Number(total.write_5m ?? 0) + Number(total.write_1h ?? 0);
  const uncached = Number(total.uncached ?? 0);

  if (reads > 0) {
    return ['in-use',
      `${(reads / 1e6).toFixed(1)}M read token(s) against ${(writes / 1e6).toFixed(1)}M ` +
      'written. Caching is on here; whether it earns its keep is the write to ' +
      'read ratio, which is a separate question.'];
  }
  if (writes > 0) {
    return ['writes-only',
      `${(writes / 1e6).toFixed(1)}M cache write token(s) and not one read. Caching ` +
      'is switched on and paying nothing back, which costs more than leaving it ' +
      'off: a write is 1.25x (5m) or 2x (1h) base input, an uncached call is 1x.'];
  }
  if (uncached < minInput) {
    return ['too-little-traffic',
      `only ${uncached} uncached input token(s) in the window; too little to ` +
      'conclude anything'];
  }
  return ['never-used',
    `${(uncached / 1e6).toFixed(1)}M uncached input token(s), zero cache reads and ` +
    'zero cache writes. Caching has never been switched on for this workload.'];
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

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. */
export function windowStart(days, now = new Date()) {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return new Date(midnight - days * 86400000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function main() {
  const adminKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!adminKey) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minInput = Number((process.env.MIN_INPUT || "dummy-min-input") ?? 1_000_000);
  const reusable = Number((process.env.REUSABLE || "dummy-reusable") ?? 0.5);

  const params = {
    starting_at: windowStart(days),
    bucket_width: '1d',
    limit: Math.min(days + 1, 31),
    'group_by[]': ['model', 'workspace_id'],
  };

  const workloads = new Map();
  for await (const bucket of buckets(adminKey, '/organizations/usage_report/messages',
                                     params)) {
    for (const result of bucket.results ?? []) {
      const name = `${result.model ?? 'all models'} / ` +
                   `${result.workspace_id ?? 'default workspace'}`;
      workloads.set(name, accumulate([result], workloads.get(name)));
    }
  }

  if (workloads.size === 0) {
    console.log(`no message usage in the last ${days} day(s)`);
    return;
  }

  let off = 0;
  const ordered = [...workloads.entries()].sort((a, b) => b[1].uncached - a[1].uncached);
  for (const [name, total] of ordered) {
    const [state, detail] = verdict(total, minInput);
    const line = `${state.padEnd(18)} ${name}  ${detail}`;
    if (state === 'in-use' || state === 'too-little-traffic') { console.log(line); continue; }
    off += 1;
    console.warn(line);
    if (state === 'never-used') {
      const ceiling = cacheSavingCeiling(total.uncached, reusable);
      console.warn(`  at ${(reusable * 100).toFixed(0)}% reusable prefix that is up to ` +
                   `${(ceiling / 1e6).toFixed(1)}M base rate input token(s) a window ` +
                   'you would stop paying for');
      console.warn('  repair: add cache_control {"type": "ephemeral"} at the end of ' +
                   'the stable prefix, keep everything variable after it, redeploy, ' +
                   'then re-read this window tomorrow');
    } else {
      console.warn('  repair: caching is already on here. Move the breakpoint to the ' +
                   'end of the stable prefix so entries get read back, or remove it: ' +
                   'paying to write and never read is worse than not caching');
    }
  }

  console.log(`${workloads.size} workload(s), ${off} with caching switched off`);
  process.exitCode = off ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
