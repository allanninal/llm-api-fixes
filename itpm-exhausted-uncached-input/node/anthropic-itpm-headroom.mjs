/**
 * Report an Anthropic input limiter that is full of uncached input.
 *
 * Read only. Two GET requests and nothing else against the Admin API, which
 * needs an Admin API key (sk-ant-admin...); a workspace key is rejected by
 * every /v1/organizations/* path. The repair is printed, never performed.
 *
 * The messages usage report carries token sums and no request count, so
 * nothing here is expressed per request.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// The one family where cache reads are charged against the input limiter.
const CACHE_READS_CHARGED = ['claude-3-5-haiku'];

const FINDINGS = new Set(['itpm-saturated-uncached', 'itpm-saturated-already-cached',
                          'itpm-saturated-cache-counts']);

/**
 * Do cache reads count toward this model's ITPM? Pure.
 * True only for Claude Haiku 3.5; getting it backwards tells a reader to add a
 * breakpoint that buys them no headroom.
 */
export function cacheReadsCount(model) {
  const name = String(model ?? '').trim().toLowerCase();
  return CACHE_READS_CHARGED.some((prefix) => name.startsWith(prefix));
}

/**
 * Tokens in one usage result that count against ITPM. Pure.
 * cache_creation is nested; a flat read sums zero and reports a heavily cached
 * workload as one that writes nothing.
 */
export function chargeableInput(result, model) {
  if (!result || typeof result !== 'object') return 0;
  const num = (v) => (Number.isFinite(Number(v)) ? Math.trunc(Number(v)) : 0);
  const creation = result.cache_creation ?? {};
  let total = num(result.uncached_input_tokens)
    + num(creation.ephemeral_5m_input_tokens)
    + num(creation.ephemeral_1h_input_tokens);
  if (cacheReadsCount(model)) total += num(result.cache_read_input_tokens);
  return total;
}

/**
 * Fold one-minute buckets into per-model peaks. Pure.
 * The peak is the finding and the mean is not: ITPM is enforced by the minute.
 */
export function peaks(buckets) {
  const perMinute = new Map();
  for (const bucket of buckets ?? []) {
    const stamp = String(bucket.starting_at ?? bucket.start_time ?? '');
    for (const result of bucket.results ?? []) {
      const model = String(result.model ?? '').trim() || 'all models';
      const key = `${model}\u0000${stamp}`;
      const row = perMinute.get(key) ?? { model, charged: 0, read: 0 };
      row.charged += chargeableInput(result, model);
      const read = Number(result.cache_read_input_tokens ?? 0);
      row.read += Number.isFinite(read) ? Math.trunc(read) : 0;
      perMinute.set(key, row);
    }
  }

  const out = {};
  for (const [key, row] of perMinute) {
    const stamp = key.slice(key.indexOf('\u0000') + 1);
    const stats = out[row.model] ?? { peak: 0, peak_at: null, peak_read: 0,
                                      minutes: 0, charged: 0, read: 0 };
    stats.minutes += 1;
    stats.charged += row.charged;
    stats.read += row.read;
    if (row.charged > stats.peak) {
      stats.peak = row.charged;
      stats.peak_at = stamp;
      stats.peak_read = row.read;
    }
    out[row.model] = stats;
  }
  return out;
}

/**
 * Share of the peak minute's input that arrived as a cache read. Pure.
 * The denominator differs on a model that charges reads, because the peak
 * already contains them.
 */
export function cacheReadShare(stats, model) {
  if (!stats || typeof stats !== 'object') return null;
  const read = Number(stats.peak_read ?? 0);
  const charged = Number(stats.peak ?? 0);
  const total = cacheReadsCount(model) ? charged : charged + read;
  if (!(total > 0)) return null;
  return Math.min(1, read / total);
}

/**
 * How much total input one ITPM ceiling carries at a given read share. Pure.
 * 1 / (1 - share): at 0.8 the same ceiling carries five times the input.
 */
export function headroomMultiplier(share) {
  if (share === null || share === undefined) return null;
  const bounded = Math.max(0, Math.min(0.99, Number(share)));
  return 1 / (1 - bounded);
}

/**
 * {model_group: input_tokens_per_minute} from the rate-limits response. Pure.
 * An omitted type is recorded as null: absent means it inherits, not unlimited.
 */
export function itpmByGroup(payload) {
  const out = {};
  for (const entry of (payload ?? {}).data ?? []) {
    const group = String(entry.model_group ?? '').trim();
    if (!group) continue;
    if (!(group in out)) out[group] = null;
    for (const limit of entry.limits ?? []) {
      if (String(limit.type ?? '').trim() !== 'input_tokens_per_minute') continue;
      const value = Number(limit.value);
      out[group] = Number.isInteger(value) ? value : null;
    }
  }
  return out;
}

/** The ITPM for the group a model id belongs to, or null. Pure. Longest prefix wins. */
export function limitFor(groups, model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return null;
  let bestKey = null;
  let bestLen = -1;
  for (const group of Object.keys(groups ?? {})) {
    const candidate = group.trim().toLowerCase();
    if (!candidate) continue;
    if (name === candidate || name.startsWith(candidate)) {
      if (candidate.length > bestLen) { bestKey = group; bestLen = candidate.length; }
    }
  }
  if (bestKey === null) return null;
  const value = groups[bestKey];
  return value === undefined ? null : value;
}

/**
 * Classify one model's input limiter. Pure. Returns [state, detail].
 * Three ways an ITPM ceiling can be full, and they do not share a repair.
 */
export function verdict(model, stats, limit, {
  floor = 0.9, watch = 0.6, minMinutes = 10, cachedEnough = 0.15,
} = {}) {
  const minutes = Number((stats ?? {}).minutes ?? 0);
  if (minutes < minMinutes) {
    return ['too-few-buckets',
      `${minutes} minute(s) of traffic in the window, under the floor of ` +
      `${minMinutes}. A peak taken over this little is noise.`];
  }
  if (limit === null || limit === undefined || limit <= 0) {
    return ['no-limit-published',
      "no input_tokens_per_minute is published for this model's group, so there " +
      'is no ceiling to compare the peak against. The limiter still exists; the ' +
      'number was simply not returned.'];
  }

  const peak = Number(stats.peak ?? 0);
  const used = peak / limit;
  const share = cacheReadShare(stats, model);
  const shape = `peak minute charged ${peak} token(s) against an ITPM of ` +
    `${limit} (${(used * 100).toFixed(0)}%); cache reads were ` +
    `${((share ?? 0) * 100).toFixed(0)}% of that minute's input`;

  if (used < watch) return ['itpm-headroom', `${shape}.`];
  if (used < floor) {
    return ['itpm-approaching',
      `${shape}. Thin enough that an ordinary spike lands on the input limiter ` +
      'rather than on the request limiter.'];
  }
  if (cacheReadsCount(model)) {
    return ['itpm-saturated-cache-counts',
      `${shape}. This model charges cache reads against ITPM, so caching lowers ` +
      'the bill here and buys no headroom at all. The levers are a shorter ' +
      'prefix or a higher limit.'];
  }
  if (share !== null && share >= cachedEnough) {
    return ['itpm-saturated-already-cached',
      `${shape}. The prefix is already being read back, so a breakpoint has ` +
      'little left to give. What remains is a limit increase, or splitting the ' +
      'workload across model groups.'];
  }
  return ['itpm-saturated-uncached',
    `${shape}. Cache reads are not charged against ITPM on this model, so ` +
    'covering the stable prefix buys throughput and not only a discount.'];
}

/** Floor to the minute: starting_at must sit on a bucket boundary. */
export function windowStart(minutes, now = new Date()) {
  const floored = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
                           now.getUTCHours(), now.getUTCMinutes());
  return new Date(floored - minutes * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function get(adminKey, path, params = {}) {
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

async function* readBuckets(adminKey, path, params) {
  const q = { ...params };
  for (;;) {
    const page = await get(adminKey, path, q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q.page = page.next_page;
  }
}

async function main() {
  const adminKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!adminKey) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const minutes = Math.max(1, Math.min(Number((process.env.MINUTES || "dummy-minutes") ?? 240), 1440));
  const targetShare = Number((process.env.TARGET_SHARE || "dummy-target-share") ?? 0.8);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const collected = [];
  for await (const bucket of readBuckets(adminKey, '/organizations/usage_report/messages',
    { starting_at: windowStart(minutes), bucket_width: '1m', limit: minutes,
      'group_by[]': ['model'] })) {
    collected.push(bucket);
  }
  const stats = peaks(collected);
  const models = Object.keys(stats);
  if (models.length === 0) {
    console.log(`no message usage in the last ${minutes} minute(s)`);
    return;
  }

  const groups = itpmByGroup(await get(adminKey, '/organizations/rate_limits'));

  let bad = 0;
  models.sort((a, b) => stats[b].peak - stats[a].peak);
  for (const model of models) {
    const row = stats[model];
    const limit = limitFor(groups, model);
    const [state, detail] = verdict(model, row, limit);
    const line = `${state.padEnd(30)} ${model.padEnd(28)} ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      if (state === 'itpm-saturated-uncached') {
        const share = cacheReadShare(row, model) ?? 0;
        console.warn(`  at this read share the ceiling carries ` +
                     `${headroomMultiplier(share).toFixed(1)}x your total input; at ` +
                     `${(targetShare * 100).toFixed(0)}% it would carry ` +
                     `${headroomMultiplier(targetShare).toFixed(1)}x`);
        console.warn('  repair: put a cache_control breakpoint at the end of the ' +
                     'stable prefix. The render order is tools, then system, then ' +
                     'messages, so the breakpoint goes after the last thing that ' +
                     'never changes.');
      } else {
        console.warn('  repair: request an input_tokens_per_minute increase for this ' +
                     'model group, or move latency tolerant work onto the Message ' +
                     'Batches API, which is metered by its own limiter group.');
      }
    } else if (state === 'itpm-approaching' || state === 'no-limit-published') {
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${models.length} model(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
