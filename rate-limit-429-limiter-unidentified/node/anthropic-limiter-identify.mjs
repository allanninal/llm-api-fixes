/**
 * Name which Anthropic rate limiter is binding, instead of catching 429.
 *
 * Read only. Two GET requests and nothing else. ANTHROPIC_API_KEY is a
 * workspace key used for a single probe against /v1/models, which generates no
 * tokens and bills nothing; ANTHROPIC_ADMIN_KEY is an Admin API key used for
 * the configured limits, because /v1/organizations/* rejects a workspace key.
 *
 * Nothing here provokes a 429.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// The three limiters that empty independently. "tokens" is not a fourth bucket:
// it reports whichever of the two token buckets is most restrictive.
const NAMED = ['requests', 'input-tokens', 'output-tokens'];
const AGGREGATE = 'tokens';

const LIMITER_TYPES = ['requests_per_minute', 'input_tokens_per_minute',
                       'output_tokens_per_minute'];

const FINDINGS = new Set(['disagreement', 'aggregate-unmatched', 'headers-missing']);

/**
 * Read a limit or remaining header as an integer. Pure, null if unreadable.
 * null and 0 stay distinct: 0 is an empty bucket, null is a stripped header.
 */
export function parseCount(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().replace(/[,_]/g, '');
  if (!text) return null;
  const n = Number(text);
  return Number.isInteger(n) ? n : null;
}

/** Parse the anthropic-ratelimit-* triples off one response. Pure. */
export function readTriples(headers) {
  const lower = new Map();
  const entries = typeof headers?.entries === 'function'
    ? [...headers.entries()] : Object.entries(headers ?? {});
  for (const [name, value] of entries) lower.set(String(name).trim().toLowerCase(), value);

  const out = {};
  for (const name of [...NAMED, AGGREGATE]) {
    const limitH = `anthropic-ratelimit-${name}-limit`;
    const remainingH = `anthropic-ratelimit-${name}-remaining`;
    const resetH = `anthropic-ratelimit-${name}-reset`;
    if (!lower.has(limitH) && !lower.has(remainingH)) continue;
    const reset = lower.get(resetH);
    out[name] = {
      limit: parseCount(lower.get(limitH)),
      remaining: parseCount(lower.get(remainingH)),
      reset: reset === null || reset === undefined ? null : String(reset).trim(),
    };
  }
  return out;
}

/**
 * Seconds until an RFC 3339 reset stamp. Pure; the caller supplies now.
 * Returns null when the stamp cannot be read rather than guessing.
 */
export function secondsUntil(value, now) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text) return null;
  const when = Date.parse(text);
  if (Number.isNaN(when)) return null;
  return (when - now.getTime()) / 1000;
}

/** remaining / limit for one triple, or null. Pure. */
export function shareLeft(triple) {
  if (!triple || typeof triple !== 'object') return null;
  const { limit, remaining } = triple;
  if (limit === null || limit === undefined) return null;
  if (remaining === null || remaining === undefined) return null;
  if (limit <= 0) return null;
  return Math.max(0, Math.min(1, remaining / limit));
}

/**
 * Which named token limiter the aggregate triple is reporting. Pure.
 * The aggregate ceiling equals the input ceiling or the output ceiling, so
 * matching it back is the platform naming the binding bucket for you.
 */
export function mirrors(parsed) {
  const limit = (parsed ?? {})[AGGREGATE]?.limit;
  if (limit === null || limit === undefined) return 'no-aggregate';
  const matched = [];
  for (const name of ['input-tokens', 'output-tokens']) {
    const other = (parsed ?? {})[name]?.limit;
    if (other !== null && other !== undefined && other === limit) matched.push(name);
  }
  if (matched.length === 2) return 'both';
  if (matched.length === 1) return matched[0];
  return 'unmatched';
}

/**
 * The named bucket with the least left. Pure. Returns [name, share].
 * The aggregate is excluded: it duplicates one of the named buckets.
 */
export function emptiest(parsed) {
  let best = null;
  for (const name of NAMED) {
    const share = shareLeft((parsed ?? {})[name]);
    if (share === null) continue;
    if (best === null || share < best[1]) best = [name, share];
  }
  return best;
}

/** Say which limiter is binding, and when the two answers disagree. Pure. */
export function verdict(parsed) {
  if (!parsed || Object.keys(parsed).length === 0) {
    return ['headers-missing',
      'no anthropic-ratelimit-* headers reached this process, so a 429 here ' +
      'would arrive with nothing to classify it by and retry-after would be ' +
      'missing too'];
  }
  const scarce = emptiest(parsed);
  if (scarce === null) {
    return ['unreadable',
      'the named triples arrived without a usable limit and remaining pair, ' +
      'so there is no ratio to compare'];
  }
  const [name, share] = scarce;
  const shape = `${name} is the emptiest named bucket at ` +
                `${(share * 100).toFixed(0)}% remaining`;
  const mirror = mirrors(parsed);

  if (mirror === 'no-aggregate') {
    return ['no-aggregate',
      `${shape}, and the aggregate anthropic-ratelimit-tokens triple is absent, ` +
      "so the platform's own view of the most restrictive token limit is not " +
      'available on this response.'];
  }
  if (mirror === 'unmatched') {
    return ['aggregate-unmatched',
      `${shape}, but the aggregate token ceiling matches neither the input nor ` +
      'the output ceiling. A third and lower limit is in effect: a workspace ' +
      'override, or a different limiter group than the one this probe touched.'];
  }
  if (mirror === 'both') {
    return ['identified',
      `${shape}, and the aggregate ceiling equals both token ceilings, so input ` +
      'and output share a number here and only the remaining counters tell ' +
      'them apart.'];
  }
  if (mirror === name) {
    return ['identified',
      `${shape}, and the aggregate ceiling mirrors ${mirror}. The tightest ` +
      'ceiling and the emptiest bucket are the same limiter.'];
  }
  return ['disagreement',
    `${shape}, while the aggregate ceiling mirrors ${mirror}. The tightest ` +
    'ceiling and the emptiest bucket are different limiters, so a handler that ' +
    'records only one of them will name the wrong cause.'];
}

/**
 * Fold GET /v1/organizations/rate_limits into {model_group: {type: value}}. Pure.
 * A limiter type missing from limits[] inherits rather than being unlimited, so
 * it is recorded as null and printed as unpublished.
 */
export function configured(payload) {
  const out = {};
  for (const entry of (payload ?? {}).data ?? []) {
    const group = String(entry.model_group ?? '').trim();
    if (!group) continue;
    if (!out[group]) {
      out[group] = {};
      for (const t of LIMITER_TYPES) out[group][t] = null;
    }
    for (const limit of entry.limits ?? []) {
      const kind = String(limit.type ?? '').trim();
      if (!(kind in out[group])) continue;
      const value = Number(limit.value);
      out[group][kind] = Number.isInteger(value) ? value : null;
    }
  }
  return out;
}

/**
 * The header names a 429 handler should be recording. Pure.
 * Built from what actually arrived, so the printed repair never tells a reader
 * to log a header their gateway is stripping.
 */
export function logHeaders(headers) {
  const lower = new Set();
  const entries = typeof headers?.entries === 'function'
    ? [...headers.entries()] : Object.entries(headers ?? {});
  for (const [name] of entries) lower.add(String(name).trim().toLowerCase());

  const wanted = new Set();
  for (const name of [...NAMED, AGGREGATE]) {
    for (const suffix of ['limit', 'remaining', 'reset']) {
      const candidate = `anthropic-ratelimit-${name}-${suffix}`;
      if (lower.has(candidate)) wanted.add(candidate);
    }
  }
  for (const extra of ['retry-after', 'request-id', 'anthropic-organization-id']) {
    if (lower.has(extra)) wanted.add(extra);
  }
  return [...wanted].sort();
}

/** One cheap real call with the workspace key. Generates nothing. */
async function probe(key) {
  const res = await fetch(`${API}/models`, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY must be a ` +
                    'workspace or project key');
  }
  if (res.status === 429) {
    console.warn('the probe itself was rate limited; the headers below describe ' +
                 'the bucket that rejected it');
    return res.headers;
  }
  if (!res.ok) throw new Error(`${res.status} from /v1/models`);
  return res.headers;
}

async function adminLimits(adminKey) {
  if (!adminKey) return {};
  const res = await fetch(`${API}/organizations/rate_limits`, {
    headers: { 'x-api-key': adminKey, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    console.warn(`${res.status} from the Admin API: /v1/organizations/* needs an ` +
                 'Admin key (sk-ant-admin...). Continuing on headers alone.');
    return {};
  }
  if (!res.ok) throw new Error(`${res.status} from /v1/organizations/rate_limits`);
  return configured(await res.json());
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY (a workspace key) for the probe');
    process.exitCode = 2;
    return;
  }
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const headers = await probe(key);
  const parsed = readTriples(headers);
  const [state, detail] = verdict(parsed);
  const line = `${state.padEnd(20)} ${detail}`;
  if (FINDINGS.has(state)) console.warn(line); else console.log(line);

  const now = new Date();
  if (showAll) {
    for (const name of Object.keys(parsed).sort()) {
      const triple = parsed[name];
      const until = secondsUntil(triple.reset, now);
      console.log(`  ${name.padEnd(14)} limit ${triple.limit}, remaining ` +
                  `${triple.remaining}, resets ` +
                  `${until === null ? 'unreadable' : `in ${until.toFixed(0)}s`}`);
    }
  }

  const groups = await adminLimits((process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key"));
  const names = Object.keys(groups).sort();
  for (const group of names) {
    const row = groups[group];
    const show = (v) => (v === null ? 'unpublished' : String(v));
    console.log(`  ${group.padEnd(24)} rpm ${show(row.requests_per_minute)}  ` +
                `itpm ${show(row.input_tokens_per_minute)}  ` +
                `otpm ${show(row.output_tokens_per_minute)}`);
  }
  if (names.length === 0) {
    console.log('  no configured limits read; set ANTHROPIC_ADMIN_KEY to name the ' +
                "ceilings per model group rather than only the probe's");
  }

  const toLog = logHeaders(headers);
  if (toLog.length > 0) {
    console.warn('  repair: record these on every 429 instead of catching a broad ' +
                 `status error: ${toLog.join(', ')}`);
    console.warn('  repair: branch before sleeping. No retry-after plus ' +
                 'error.details.error_code of enforced_spend_limit_reached is a ' +
                 'billing stop, not a throttle, and will not clear.');
  } else {
    console.warn('  repair: no rate-limit headers arrived at all. Check the proxy ' +
                 'or gateway in front of api.anthropic.com and let the ' +
                 'anthropic-ratelimit-* and retry-after headers through.');
  }

  process.exitCode = FINDINGS.has(state) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
