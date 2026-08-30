/**
 * Report how much OpenAI rate-limit headroom is left, before anything 429s.
 *
 * Read only. One GET request and nothing else: OPENAI_API_KEY should be a
 * project key set to Read Only. GET /v1/models consumes no inference quota and
 * carries the same x-ratelimit-* header set as a completion, which is the whole
 * trick, because OpenAI has no endpoint that returns remaining quota.
 *
 * The repair is printed, never performed, and the script never tries to provoke
 * a 429.
 */
const API = 'https://api.openai.com/v1';

const DIMENSIONS = ['requests', 'tokens', 'project-requests', 'project-tokens'];

// "ms" before "m", because the other order parses 500ms as 500 minutes.
const DURATION = /(\d+(?:\.\d+)?)(ms|us|ns|h|m|s)/g;
const UNITS = { ns: 1e-9, us: 1e-6, ms: 1e-3, s: 1, m: 60, h: 3600 };

const FINDINGS = new Set(['exhausted', 'near-exhaustion']);

/** The limit/remaining/reset header triple for one dimension. Pure. */
export function headerNames(dimension) {
  return [`x-ratelimit-limit-${dimension}`,
          `x-ratelimit-remaining-${dimension}`,
          `x-ratelimit-reset-${dimension}`];
}

/**
 * Read a limit or remaining header as an integer. Pure.
 * Returns null rather than 0 when absent: zero is a real state here and means
 * the bucket is empty, so folding the two reports a stripped header as an
 * exhausted limiter.
 */
export function parseCount(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().replace(/[,_]/g, '');
  if (!text) return null;
  const n = Number(text);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/**
 * Read a reset header as seconds. Pure. Returns null if unreadable.
 * The whole string must be consumed; a partial match on "60 seconds" would
 * return a number from a format this parser does not understand.
 */
export function parseReset(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().toLowerCase();
  if (!text) return null;
  const parts = [...text.matchAll(DURATION)];
  if (parts.length === 0) return null;
  if (parts.map(([whole]) => whole).join('') !== text) return null;
  return parts.reduce((sum, [, n, unit]) => sum + Number(n) * UNITS[unit], 0);
}

/**
 * Parse the x-ratelimit-* triples off one response. Pure.
 * Case-insensitive, because gateways rewrite header casing freely.
 */
export function triples(headers) {
  const lower = new Map();
  const entries = typeof headers?.entries === 'function'
    ? [...headers.entries()] : Object.entries(headers ?? {});
  for (const [name, value] of entries) lower.set(String(name).trim().toLowerCase(), value);

  const out = {};
  for (const dimension of DIMENSIONS) {
    const [limitH, remainingH, resetH] = headerNames(dimension);
    if (!lower.has(limitH) && !lower.has(remainingH)) continue;
    out[dimension] = {
      limit: parseCount(lower.get(limitH)),
      remaining: parseCount(lower.get(remainingH)),
      reset: parseReset(lower.get(resetH)),
    };
  }
  return out;
}

/** remaining / limit for one dimension, or null. Pure. */
export function headroom(triple) {
  if (!triple || typeof triple !== 'object') return null;
  const { limit, remaining } = triple;
  if (limit === null || limit === undefined) return null;
  if (remaining === null || remaining === undefined) return null;
  if (limit <= 0) return null;
  return Math.max(0, Math.min(1, remaining / limit));
}

/** Classify one dimension. Pure. Returns [state, detail]. */
export function verdict(dimension, triple, floor = 0.2) {
  const share = headroom(triple);
  if (share === null) {
    return ['unreadable',
      `the ${dimension} triple arrived without a usable limit and remaining ` +
      'pair, so there is no ratio to read'];
  }
  const { remaining, limit, reset } = triple;
  const window = reset === null || reset === undefined
    ? 'no readable reset' : `resets in ${reset.toFixed(0)}s`;
  const shape = `${remaining} of ${limit} left (${(share * 100).toFixed(0)}%), ${window}`;

  if (remaining === 0) {
    return ['exhausted',
      `${shape}. This bucket is empty now, so the next call in this window is ` +
      'a 429 no matter how small it is.'];
  }
  if (share < floor) {
    return ['near-exhaustion',
      `${shape}. Under the ${(floor * 100).toFixed(0)}% floor, which means the ` +
      'next traffic spike converts this into a 429.'];
  }
  return ['headroom', `${shape}.`];
}

/**
 * The dimension with the least headroom left. Pure. Returns [name, share].
 * The mean of two independently emptying buckets is a number about nothing.
 */
export function binding(parsed) {
  let best = null;
  for (const dimension of Object.keys(parsed ?? {}).sort()) {
    const share = headroom(parsed[dimension]);
    if (share === null) continue;
    if (best === null || share < best[1]) best = [dimension, share];
  }
  return best;
}

/**
 * Which scope owns the real ceiling, per dimension. Pure.
 * Returns [owner, dimension, bindingLimit, otherLimit] rows.
 */
export function scopeNote(parsed) {
  const out = [];
  for (const dimension of ['requests', 'tokens']) {
    const orgLimit = (parsed ?? {})[dimension]?.limit;
    const projectLimit = (parsed ?? {})[`project-${dimension}`]?.limit;
    if (orgLimit === null || orgLimit === undefined) continue;
    if (projectLimit === null || projectLimit === undefined) continue;
    if (projectLimit < orgLimit) out.push(['project', dimension, projectLimit, orgLimit]);
    else if (orgLimit < projectLimit) out.push(['organization', dimension, orgLimit, projectLimit]);
    else out.push(['equal', dimension, projectLimit, orgLimit]);
  }
  return out;
}

/** One cheap real call. GET only, and it consumes no inference quota. */
async function probe(key) {
  const res = await fetch(`${API}/models`, {
    headers: { Authorization: `Bearer ${key}` },
  });
  if (res.status === 401) throw new Error('401 from OpenAI: OPENAI_API_KEY is not a valid key');
  if (res.status === 429) {
    console.warn('the probe itself was rate limited; the headers below describe ' +
                 'the bucket that rejected it');
    return res.headers;
  }
  if (!res.ok) throw new Error(`${res.status} from /v1/models`);
  return res.headers;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }
  const floor = Number((process.env.FLOOR || "dummy-floor") ?? 0.2);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const parsed = triples(await probe(key));
  if (Object.keys(parsed).length === 0) {
    console.warn('headers-missing    no x-ratelimit-* headers reached this process at all');
    console.warn('  This is not a clean bill of health. Something between you and ' +
                 'OpenAI is stripping response headers, so you have no forward-looking ' +
                 'signal and no Retry-After on the 429 when it arrives.');
    console.warn('  repair: check the proxy, gateway or LLM router in front of ' +
                 'api.openai.com and allow the x-ratelimit-* and retry-after headers ' +
                 'through unmodified');
    process.exitCode = 1;
    return;
  }

  let checked = 0;
  let bad = 0;
  for (const dimension of Object.keys(parsed).sort()) {
    const [state, detail] = verdict(dimension, parsed[dimension], floor);
    checked += 1;
    const line = `${state.padEnd(16)} ${dimension.padEnd(18)} ${detail}`;
    if (FINDINGS.has(state)) { bad += 1; console.warn(line); }
    else if (state === 'unreadable') console.warn(line);
    else if (showAll) console.log(line);
  }

  const scarcest = binding(parsed);
  if (scarcest) {
    console.log(`binding dimension: ${scarcest[0]}, at ` +
                `${(scarcest[1] * 100).toFixed(0)}% of its ceiling`);
  }

  for (const [owner, dimension, low, high] of scopeNote(parsed)) {
    if (owner === 'project') {
      console.warn(`  note: the project ceiling binds for ${dimension} (${low} ` +
                   `against an org ${high}), so org headroom is not your headroom`);
    } else if (owner === 'organization') {
      console.log(`  note: the org ceiling binds for ${dimension} (${low} against ` +
                  `a project ${high})`);
    }
  }

  if (bad) {
    console.warn('  repair: request a usage tier increase, or pace the client with ' +
                 'a token bucket sized to the limit above so bursts are spread ' +
                 'across the window instead of rejected');
    console.warn('  repair: to raise the project ceiling instead, an admin can call ' +
                 'POST /v1/organization/projects/{project_id}/rate_limits/{rate_limit_id}. ' +
                 'That is a write against a limit your colleagues share, so it is ' +
                 'printed, not run.');
  }

  console.log(`${checked} dimension(s) read, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
