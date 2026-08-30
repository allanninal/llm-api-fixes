/**
 * Tell an OpenAI billing wall apart from a real rate limit, before it stops you.
 *
 * Read only. GET requests and nothing else: OPENAI_ADMIN_KEY is an organization
 * admin key with read scopes, OPENAI_API_KEY is an optional Read Only project
 * key used for a live probe. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// 429 codes that describe money rather than traffic. None clears on retry.
export const WALL = {
  insufficient_quota:
    'no usable balance. This is the older name for the same wall and is still ' +
    'what many accounts return; add credits or enable auto-recharge.',
  credit_balance_exhausted:
    'prepaid credits are gone. Add credits or enable auto-recharge.',
  organization_spend_limit_exceeded:
    'the monthly spend limit you set on the organization was reached. Raise it, ' +
    'or wait for the interval to reset.',
  project_spend_limit_exceeded:
    'the spend limit set on this project was reached. Raise it on the project, ' +
    'not on the organization.',
  organization_usage_limit_exceeded:
    'the ceiling OpenAI assigns your usage tier was reached. Nothing you own ' +
    'can raise this; request an increase from OpenAI.',
};

const THROTTLE = ['rate_limit_exceeded', 'requests_limit_reached', 'tokens_limit_reached'];

export const TIER_LIMIT = { 1: 100, 2: 500, 3: 1000, 4: 5000, 5: 200000 };

/**
 * Return [code, type, message] from either provider's error envelope. Empty
 * strings when absent, so callers never guard three levels of property access.
 */
export function errorFields(body) {
  if (!body || typeof body !== 'object') return ['', '', ''];
  const err = (body.error && typeof body.error === 'object') ? body.error : body;
  return [String(err.code ?? ''), String(err.type ?? ''), String(err.message ?? '')];
}

/**
 * Decide whether an error may be retried. Pure, so it is testable offline and
 * can be lifted straight into a retry wrapper. Returns [state, detail]. Only
 * 'throttle' and 'transient' are safe to retry.
 */
export function classify(status, body) {
  const [code, etype, message] = errorFields(body);
  const low = message.toLowerCase();

  if (status === 429) {
    if (Object.hasOwn(WALL, code)) {
      return ['wall',
        `${code}: ${WALL[code]} Retrying cannot clear this, and the SDK still ` +
        'raises RateLimitError for it.'];
    }
    if (THROTTLE.includes(code)) {
      return ['throttle',
        `${code}: a real limit on how fast you may send. Back off and honour ` +
        'Retry-After.'];
    }
    if (!code) {
      if (etype === 'rate_limit_error') {
        return ['throttle',
          'Anthropic 429 rate_limit_error. It carries no code field, so match ' +
          'on type here rather than on code.'];
      }
      return ['unclassified-429',
        '429 with no code and no recognised type. Retry once, then fail loudly: ' +
        'an unbounded loop against a wall is worse than a page.'];
    }
    return ['unclassified-429',
      `429 with unrecognised code ${code}. Treat as not retryable until ` +
      'somebody has read it.'];
  }

  if (status === 400 && low.includes('credit balance')) {
    return ['wall',
      'Anthropic reports an exhausted balance as a 400 invalid_request_error, ' +
      'not a 429. There is no code field to branch on, so the message is the ' +
      'only signal available; it is a fragile match and worth an alert of its ' +
      'own when it fires.'];
  }

  if (status === 401 || status === 403) {
    return ['auth',
      `status ${status}: the key is wrong, revoked, or scoped away from this ` +
      'endpoint. Retrying will not mint a new one.'];
  }

  if (status >= 500 || status === 408) {
    return ['transient', `status ${status}: server side. Retry with backoff.`];
  }

  return ['other', `status ${status}, code ${code || 'none'}`];
}

/** Compare month-to-date spend against a tier ceiling. Pure. */
export function headroom(spent, limit) {
  if (limit === null || limit === undefined) {
    return ['tier-unknown',
      `$${spent.toFixed(2)} spent this month. Pass --tier to compare it against ` +
      'the ceiling OpenAI assigns that tier; the API does not expose which tier ' +
      'you are on.'];
  }
  if (spent >= limit) {
    return ['at-ceiling',
      `$${spent.toFixed(2)} of a $${limit.toFixed(2)} monthly ceiling. Inference ` +
      'is returning, or is about to return, 429 organization_usage_limit_exceeded.'];
  }
  if (spent >= limit * 0.8) {
    return ['approaching',
      `$${spent.toFixed(2)} of a $${limit.toFixed(2)} monthly ceiling ` +
      `(${((spent / limit) * 100).toFixed(0)}%). This is the one wall you can ` +
      'forecast to the day.'];
  }
  return ['clear', `$${spent.toFixed(2)} of a $${limit.toFixed(2)} monthly ceiling`];
}

/**
 * Find a cliff in the aggregate usage buckets. Pure, clock passed in.
 *
 * There is no per-request log on either API, so a wall that has already been hit
 * is not visible as an error rate. It is visible as traffic that stops.
 * Returns [state, detail].
 */
export function stalled(buckets, now, quietHours = 6) {
  const rows = [];
  for (const b of buckets) {
    let reqs = 0;
    let out = 0;
    for (const r of b.results ?? []) {
      reqs += Number(r.num_model_requests ?? 0);
      out += Number(r.output_tokens ?? 0);
    }
    if (typeof b.start_time === 'number') rows.push([b.start_time, reqs, out]);
  }
  rows.sort((a, b) => a[0] - b[0]);

  if (rows.length === 0) return ['no-data', 'no usage buckets returned for this window'];

  const busy = rows.filter((r) => r[1] > 0);
  if (busy.length === 0) {
    return ['no-data',
      `${rows.length} bucket(s), none with a single model request. Either ` +
      'nothing ran, or the wall predates the window.'];
  }

  const barren = busy.filter((r) => r[2] === 0);
  if (barren.length > 0) {
    return ['failing-before-generation',
      `${barren.length} bucket(s) with requests but zero output tokens. Those ` +
      'calls did not generate: they were rejected before the model ran. That is ' +
      'an error shape, not a spend shape.'];
  }

  const age = (now.getTime() / 1000 - busy[busy.length - 1][0]) / 3600;
  if (age >= quietHours) {
    return ['cliff',
      `last model request ${age.toFixed(1)} hour(s) ago and nothing since. ` +
      'Traffic stopping dead mid-cycle is what a billing wall looks like from ' +
      'the usage API, because there is no error log to read.'];
  }
  return ['flowing', `traffic in the last ${age.toFixed(1)} hour(s)`];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization endpoints need ` +
                    'an organization admin key, not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read ' +
                  'scopes; project keys are rejected by /v1/organization/*)');
    process.exitCode = 2;
    return;
  }

  const argv = process.argv;
  const tier = Number(argv.includes('--tier') ? argv[argv.indexOf('--tier') + 1] : 0) || 0;
  const hours = Number(argv.includes('--hours') ? argv[argv.indexOf('--hours') + 1] : 48) || 48;

  const now = new Date();
  const monthStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1) / 1000;

  const costs = await get(admin, '/organization/costs',
    { start_time: Math.floor(monthStart), bucket_width: '1d', limit: 31 });
  let spent = 0;
  for (const b of costs.data ?? []) {
    for (const r of b.results ?? []) spent += Number(r.amount?.value ?? 0);
  }

  let bad = 0;
  {
    const [state, detail] = headroom(spent, TIER_LIMIT[tier] ?? null);
    if (state === 'clear' || state === 'tier-unknown') {
      console.log(`${state.padEnd(13)} ${detail}`);
    } else {
      bad += 1;
      console.warn(`${state.padEnd(13)} ${detail}`);
      console.warn('  repair: add prepaid credits, raise the org or project spend ' +
                   'limit, or ask OpenAI for a higher approved usage limit. Which ' +
                   'one depends on the error code, not the status.');
    }
  }

  const since = Math.floor(now.getTime() / 1000 - hours * 3600);
  const usage = await get(admin, '/organization/usage/completions',
    { start_time: since, bucket_width: '1h', limit: Math.max(hours, 1) });
  const buckets = usage.data ?? [];
  {
    const [state, detail] = stalled(buckets, now);
    if (state === 'flowing') console.log(`${state.padEnd(13)} ${detail}`);
    else { bad += 1; console.warn(`${state.padEnd(13)} ${detail}`); }
  }

  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (key) {
    const res = await fetch(`${API}/models`, { headers: { Authorization: `Bearer ${key}` } });
    if (res.ok) {
      console.log(`probe         GET /v1/models answered 200; headroom ` +
                  `${res.headers.get('x-ratelimit-remaining-requests') ?? 'not reported'}`);
    } else {
      const body = await res.json().catch(() => ({}));
      const [state, detail] = classify(res.status, body);
      bad += 1;
      console.warn(`probe         ${state}  ${detail}`);
    }
  } else {
    console.log('probe         skipped: set OPENAI_API_KEY (Read Only) to read ' +
                'rate-limit headers from a live response');
  }

  console.log(`${buckets.length} bucket(s) read over ${hours} hour(s), ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
