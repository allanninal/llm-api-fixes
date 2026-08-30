/**
 * Prove that retry-after can reach your client before you need it.
 *
 * Read only, and deliberately small: one GET /v1/models per path. This script
 * will not drive traffic into a 429 in order to photograph one.
 *
 * retry-after appears only on a 429, so its class is probed instead: the rate
 * limit triples arrive on every response and are forwarded or dropped by the
 * same middlebox rules. Two paths, because one cannot attribute a loss. Only
 * the -limit- values are compared, because remaining and reset are supposed to
 * move between two calls a second apart.
 */
const DIRECT = {
  anthropic: 'https://api.anthropic.com/v1',
  openai: 'https://api.openai.com/v1',
};

export const REQUIRED = {
  anthropic: [
    'anthropic-ratelimit-requests-limit',
    'anthropic-ratelimit-requests-remaining',
    'anthropic-ratelimit-requests-reset',
    'anthropic-ratelimit-input-tokens-limit',
    'anthropic-ratelimit-input-tokens-remaining',
    'anthropic-ratelimit-input-tokens-reset',
    'anthropic-ratelimit-output-tokens-limit',
    'anthropic-ratelimit-output-tokens-remaining',
    'anthropic-ratelimit-output-tokens-reset',
    'anthropic-ratelimit-tokens-limit',
    'anthropic-ratelimit-tokens-remaining',
    'anthropic-ratelimit-tokens-reset',
  ],
  openai: [
    'x-ratelimit-limit-requests',
    'x-ratelimit-remaining-requests',
    'x-ratelimit-reset-requests',
    'x-ratelimit-limit-tokens',
    'x-ratelimit-remaining-tokens',
    'x-ratelimit-reset-tokens',
  ],
};

export const OPTIONAL = {
  anthropic: ['retry-after', 'request-id', 'anthropic-workspace-id',
              'anthropic-priority-input-tokens-limit',
              'anthropic-priority-output-tokens-limit'],
  openai: ['retry-after', 'x-request-id', 'x-ratelimit-limit-project-tokens',
           'x-ratelimit-remaining-project-tokens', 'x-ratelimit-reset-project-tokens'],
};

const SKEW_SECONDS = 5;
const FINDINGS = new Set(['headers-stripped', 'headers-rewritten', 'headers-absent',
                          'reset-in-the-past', 'clock-skew']);

const UNIT = { ms: 0.001, s: 1, m: 60, h: 3600 };

/** {lowercase name: value}. Pure. Middleboxes rewrite casing freely. */
export function lowerHeaders(headers) {
  const out = {};
  for (const [key, value] of Object.entries(headers ?? {})) {
    out[String(key).trim().toLowerCase()] = String(value);
  }
  return out;
}

/** Required header names absent from this response. Pure. Sorted. */
export function missing(headers, provider) {
  const present = lowerHeaders(headers);
  return (REQUIRED[provider] ?? []).filter((n) => !(n in present)).sort();
}

/** {header: [direct, gateway, state]} across two paths. Pure. */
export function compare(direct, gateway, provider) {
  const left = lowerHeaders(direct);
  const right = lowerHeaders(gateway);
  const names = new Set([...(REQUIRED[provider] ?? []), ...(OPTIONAL[provider] ?? [])]);
  for (const n of [...Object.keys(left), ...Object.keys(right)]) {
    if (n.includes('ratelimit') || n === 'retry-after') names.add(n);
  }
  const out = {};
  for (const name of [...names].sort()) {
    const a = left[name];
    const b = right[name];
    let state;
    if (a === undefined && b === undefined) state = 'absent-both';
    else if (a !== undefined && b === undefined) state = 'stripped';
    else if (a === undefined && b !== undefined) state = 'added';
    else if (name.includes('-limit') && a !== b) state = 'rewritten';
    else state = 'intact';
    out[name] = [a, b, state];
  }
  return out;
}

/** [kind, seconds] for a reset header. Pure. absolute | duration | unknown. */
export function parseReset(value) {
  const text = String(value ?? '').trim();
  if (!text) return ['unknown', null];
  if (/^\d{4}-\d{2}-\d{2}[T ]/.test(text)) {
    const ms = Date.parse(text);
    if (Number.isFinite(ms)) return ['absolute', ms / 1000];
  }
  if (/^(?:\d+(?:\.\d+)?(?:ms|h|m|s))+$/.test(text)) {
    let total = 0;
    for (const [, n, u] of text.matchAll(/(\d+(?:\.\d+)?)(ms|h|m|s)/g)) {
      total += Number(n) * UNIT[u];
    }
    return ['duration', total];
  }
  const plain = Number(text);
  return Number.isFinite(plain) ? ['duration', plain] : ['unknown', null];
}

/** local clock minus the server's date header, in seconds. Pure. null if unknown. */
export function clockSkew(dateHeader, localEpoch) {
  const text = String(dateHeader ?? '').trim();
  if (!text) return null;
  const ms = Date.parse(text);
  if (!Number.isFinite(ms)) return null;
  return Number(localEpoch) - ms / 1000;
}

/** [[header, secondsInThePast]] for absolute resets already elapsed. Pure. */
export function staleResets(headers, provider, serverEpoch) {
  const present = lowerHeaders(headers);
  const out = [];
  for (const name of REQUIRED[provider] ?? []) {
    if (!name.endsWith('-reset')) continue;
    const [kind, value] = parseReset(present[name]);
    if (kind === 'absolute' && value !== null && value < serverEpoch) {
      out.push([name, serverEpoch - value]);
    }
  }
  out.sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
  return out;
}

const requiredAny = (comparison) => {
  const names = new Set();
  for (const required of Object.values(REQUIRED)) {
    for (const n of required) if (n in (comparison ?? {})) names.add(n);
  }
  return names;
};

/** Classify one provider's probe. Pure. Returns [state, detail]. */
export function verdict(comparison, directMissing, gatewayUsed, skew, stale) {
  const rows = Object.entries(comparison ?? {});
  const stripped = rows.filter(([, v]) => v[2] === 'stripped').map(([n]) => n);
  const rewritten = rows.filter(([, v]) => v[2] === 'rewritten').map(([n]) => n);
  const total = requiredAny(comparison).size;
  const absent = (directMissing ?? []).length;

  if (absent && !gatewayUsed) {
    return ['headers-absent',
            `${absent} required rate limit header(s) did not arrive at all, and `
            + 'there is no gateway configured to blame for it'];
  }
  if (stripped.length) {
    return ['headers-stripped',
            `${stripped.length} of ${Math.max(total, stripped.length)} rate limit `
            + 'header(s) do not survive the gateway'];
  }
  if (rewritten.length) {
    return ['headers-rewritten',
            `${rewritten.length} limit value(s) differ between the two paths, so `
            + 'something is generating headers rather than forwarding them'];
  }
  if (absent) {
    return ['headers-absent',
            `${absent} required rate limit header(s) are absent on both paths`];
  }
  if ((stale ?? []).length) {
    return ['reset-in-the-past',
            `${stale[0][0]} is already ${Math.round(stale[0][1])}s in the past by `
            + "the server's own clock"];
  }
  if (skew !== null && skew !== undefined && Math.abs(skew) > SKEW_SECONDS) {
    return ['clock-skew',
            `local clock is ${Math.round(Math.abs(skew))}s `
            + `${skew < 0 ? 'behind' : 'ahead of'} the server's date header`];
  }
  const intact = rows.filter(([, v]) => v[2] === 'intact').length;
  return ['headers-intact',
          `${intact} rate limit header(s) present and consistent across every path checked`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, provider = '', names = []) {
  const list = names ?? [];
  if (state === 'headers-stripped') {
    return ['retry-after travels with these. A path that drops them on a 200 drops '
      + 'the wait instruction on a 429, and your backoff falls back to a constant '
      + 'that retries into an empty bucket.',
      `add these names to the response header allowlist on the gateway: ${
        list.slice(0, 6).join(', ') || '(none recorded)'}${list.length > 6 ? ' ...' : ''}`];
  }
  if (state === 'headers-rewritten') {
    return ['a limit value that differs between two paths a second apart is not a '
      + 'live number. Find the layer caching or synthesising responses and make it '
      + "forward the origin's headers unchanged.",
      'this state is more dangerous than stripping, because the client believes the '
      + 'numbers it is given and has no way to tell.'];
  }
  if (state === 'headers-absent') {
    return ['nothing arrived on any path checked, so this is not attributable yet. '
      + 'Re-run with the gateway base URL set, and confirm the credential and '
      + 'endpoint are the ones production uses.'];
  }
  if (state === 'reset-in-the-past') {
    return ['a reset instant already in the past makes any sleep computed from it a '
      + 'no-op, so the client retries immediately and 429s again. Prefer '
      + 'retry-after, which is relative.'];
  }
  if (state === 'clock-skew') {
    if (provider === 'anthropic') {
      return ['anthropic reset values are RFC 3339 instants, so a sleep computed '
        + 'from one is only as good as clock agreement. Fix time sync on this host, '
        + 'or use retry-after instead, which is relative and immune to skew.',
        'the same skew affects any log correlation you do against these timestamps, '
        + 'which is usually how it is finally noticed.'];
    }
    return ['this provider returns reset values as durations, so backoff is '
      + 'unaffected, but the skew will still misalign every log line you correlate '
      + "against the API's timestamps."];
  }
  return [];
}

async function probeOnce(url, headers) {
  try {
    const r = await fetch(url, { headers });
    let note = '';
    if (r.status === 429) {
      note = 'a 429 arrived on its own. retry-after came back as '
             + `${JSON.stringify(r.headers.get('retry-after'))}`;
    } else if (r.status === 401 || r.status === 403) {
      note = `${r.status}: the credential cannot read this path`;
    }
    return [r.status, Object.fromEntries(r.headers.entries()), note];
  } catch (err) {
    return [null, {}, `request failed: ${err.message}`];
  }
}

const host = (url) => String(url).split('//').pop().split('/')[0];

async function audit(provider, key, baseUrl) {
  const directBase = DIRECT[provider];
  const auth = provider === 'anthropic'
    ? { 'x-api-key': key, 'anthropic-version': '2023-06-01' }
    : { Authorization: `Bearer ${key}` };
  auth['User-Agent'] = 'retry-after-header-probe/1.0';

  console.log(`${provider}: direct ${host(directBase)}, `
              + `${baseUrl ? `gateway ${host(baseUrl)}` : 'no gateway configured'}`);

  const [, directHeaders, note] = await probeOnce(`${directBase}/models`, auth);
  if (note) console.log(`  direct: ${note}`);
  let gatewayHeaders = {};
  if (baseUrl) {
    await new Promise((r) => { setTimeout(r, 1000); });
    const [, gh, gnote] = await probeOnce(`${baseUrl.replace(/\/$/, '')}/models`, auth);
    gatewayHeaders = gh;
    if (gnote) console.log(`  gateway: ${gnote}`);
  }

  const comparison = compare(directHeaders,
                             baseUrl ? gatewayHeaders : directHeaders, provider);
  const directMissing = missing(directHeaders, provider);
  const now = Date.now() / 1000;
  const skew = clockSkew(lowerHeaders(directHeaders).date, now);
  const serverEpoch = now - (skew ?? 0);
  const stale = staleResets(directHeaders, provider, serverEpoch);

  const [state, detail] = verdict(comparison, directMissing, Boolean(baseUrl),
                                  skew, stale);
  console.log(`${state.padEnd(21)} ${detail}`);

  const stripped = Object.entries(comparison)
    .filter(([, v]) => v[2] === 'stripped').map(([n]) => n);
  for (const name of stripped.slice(0, 6)) console.log(`  stripped   ${name}`);
  for (const [name, v] of Object.entries(comparison).sort()) {
    if (v[2] === 'intact' && name.endsWith('-limit') && v[0]) {
      console.log(`  intact     ${name.padEnd(42)} ${v[0]}`);
    }
  }
  for (const [name, seconds] of stale.slice(0, 3)) {
    console.log(`  stale      ${name}, ${Math.round(seconds)}s in the past`);
  }
  for (const line of repairLines(state, provider, stripped)) {
    console.log(`  repair: ${line}`);
  }
  return FINDINGS.has(state) ? 1 : 0;
}

async function main() {
  const anthropicKey = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  const openaiKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!anthropicKey && !openaiKey) {
    console.error('set ANTHROPIC_API_KEY, OPENAI_API_KEY, or both, and set the '
                  + 'matching base URL if production reaches the API through a gateway');
    process.exitCode = 2;
    return;
  }
  let findings = 0;
  if (anthropicKey) {
    findings += await audit('anthropic', anthropicKey, (process.env.ANTHROPIC_BASE_URL || "https://example.com"));
  }
  if (openaiKey) {
    findings += await audit('openai', openaiKey, (process.env.OPENAI_BASE_URL || "https://example.com"));
  }
  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
