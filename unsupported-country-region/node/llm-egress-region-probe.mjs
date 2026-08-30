/**
 * Prove whether a 403 is about where the request left from, or about the key.
 *
 * Read only. One GET of /v1/models per provider whose key is present, and
 * nothing else. No request body, nothing generated, nothing billed, and no
 * third-party service contacted: the script never looks up its own public IP,
 * because the provider's own answer is the only authority that counts.
 *
 * The variable is the machine, so the unit of evidence is a pair. Run it from
 * a host you trust, carry the one-line blob, run it again from production with
 * that line in LLM_EGRESS_BASELINE.
 */
const PROVIDERS = {
  openai: { url: 'https://api.openai.com/v1/models', env: 'OPENAI_API_KEY' },
  anthropic: { url: 'https://api.anthropic.com/v1/models', env: 'ANTHROPIC_API_KEY' },
};

// The one code treated as proof, because it is the one that is documented.
export const BLOCK_CODE = 'unsupported_country_region_territory';

const FINDINGS = new Set(['geography-isolated', 'region-blocked-unconfirmed',
  'region-blocked-everywhere', 'forbidden-unexplained']);

/** The provider's error code from a JSON body. Pure. Empty when absent. */
export function errorCode(body) {
  const error = (body && typeof body === 'object') ? body.error : null;
  if (!error || typeof error !== 'object') return '';
  for (const field of ['code', 'type']) {
    if (error[field]) return String(error[field]).trim();
  }
  return '';
}

/** One probe result, reduced. Pure. The only thing that leaves the process. */
export function observation(provider, status, body) {
  return {
    provider: String(provider),
    status: (status === null || status === undefined) ? null : Math.trunc(Number(status)),
    code: String(errorCode(body) || ''),
  };
}

/** Grade one observation. Pure. Returns [state, detail]. */
export function classify(obs) {
  const row = obs ?? {};
  const code = String(row.code ?? '');
  if (row.status === null || row.status === undefined) {
    return ['unreachable',
      'no response at all, which is a network answer rather than a policy one'];
  }
  const status = Math.trunc(Number(row.status));
  if (status === 200) return ['reachable', 'this egress path is allowed for this key'];
  if (status === 403 && code === BLOCK_CODE) return ['region-blocked', BLOCK_CODE];
  if (status === 403) {
    return ['forbidden-other',
      `403 with code '${code || '(none returned)'}', which is not the documented `
      + 'geographic block'];
  }
  if (status === 401) return ['credentials', '401, which is the key and not the location'];
  if (status === 429) {
    return ['rate-limited',
      '429, so this host reaches the provider fine and the question is capacity '
      + 'rather than geography'];
  }
  return ['unexpected', `${status} with code '${code || '(none)'}'`];
}

/** The note. Pure. Returns [state, detail]. The only two-host function. */
export function compare(local, baseline) {
  const [localState, localDetail] = classify(local);
  const provider = (local ?? {}).provider ?? '(unknown)';
  if (!baseline) {
    if (localState === 'region-blocked') {
      return ['region-blocked-unconfirmed',
        `${provider}: 403 ${BLOCK_CODE} from this host. The code is documented, `
        + 'but with no baseline this has not been separated from an '
        + 'account-level restriction'];
    }
    if (localState === 'forbidden-other' || localState === 'credentials') {
      return ['no-baseline',
        `${provider}: ${localDetail}, and no baseline to compare it against. Run `
        + 'this from a host you trust first'];
    }
    return [localState === 'reachable' ? 'clear' : localState,
            `${provider}: ${localDetail}`];
  }

  const [baseState] = classify(baseline);
  if (localState === 'reachable') {
    return ['clear', `${provider}: 200 from this host, so the egress path is fine`];
  }
  if (localState === 'region-blocked' && baseState === 'reachable') {
    return ['geography-isolated',
      `${provider}: 403 here and 200 from the baseline host on the same key, so `
      + 'the difference is the egress path and not the credential'];
  }
  if (localState === 'region-blocked' && baseState === 'region-blocked') {
    return ['region-blocked-everywhere',
      `${provider}: 403 ${BLOCK_CODE} from both hosts, so this is the account or `
      + "an organization-level restriction rather than this deployment's location"];
  }
  if (localState === 'credentials' && baseState === 'credentials') {
    return ['credentials-not-geography',
      `${provider}: 401 from both hosts on the same key, which is the credential `
      + 'and not the location'];
  }
  if (localState === 'credentials') {
    return ['credentials-here-only',
      `${provider}: 401 here and ${baseState} from the baseline host. A key that `
      + 'authenticates elsewhere and not here is usually a different key in the '
      + 'environment, not a geographic block'];
  }
  if (localState === 'forbidden-other' && baseState === 'reachable') {
    return ['forbidden-unexplained',
      `${provider}: ${localDetail} here and 200 from the baseline host. The host `
      + 'is the difference; the code is not one this script can attribute'];
  }
  return ['inconclusive',
    `${provider}: ${localState} here, ${baseState} from the baseline host`];
}

/** The one line to carry to the other host. Pure. Sorted, and no secrets. */
export function blob(observations) {
  const payload = {};
  for (const obs of observations ?? []) {
    const row = obs ?? {};
    payload[String(row.provider)] = {
      code: String(row.code ?? ''),
      status: row.status ?? null,
    };
  }
  const sorted = {};
  for (const key of Object.keys(payload).sort()) {
    const inner = payload[key];
    sorted[key] = { code: inner.code, status: inner.status };
  }
  return JSON.stringify(sorted);
}

/** {provider: observation} from the blob. Pure. Empty object on anything odd. */
export function loadBaseline(raw) {
  let parsed;
  try {
    parsed = JSON.parse(String(raw ?? '').trim() || '{}');
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
  const out = {};
  for (const [provider, row] of Object.entries(parsed)) {
    if (!row || typeof row !== 'object') continue;
    const raw_status = row.status;
    const status = (raw_status === null || raw_status === undefined
                    || !Number.isFinite(Number(raw_status)))
      ? null : Math.trunc(Number(raw_status));
    out[String(provider)] = { provider: String(provider), status,
                              code: String(row.code ?? '') };
  }
  return out;
}

/** The repair for one verdict. Pure. A region pin, never a proxy. */
export function repairLines(state) {
  const pin = "pin execution to a supported region. On Vercel, export const "
    + "config = { regions: ['iad1'] }. On Cloud Run, Lambda or a container, "
    + "redeploy in a supported region. On a VPN, turn it off.";
  const noProxy = 'do not route the provider host through another egress to get '
    + 'around this. Move the workload, not the packets.';
  if (state === 'geography-isolated') return [pin, noProxy];
  if (state === 'region-blocked-unconfirmed') {
    return ['run this same script from a host you already trust and paste its '
      + 'blob into LLM_EGRESS_BASELINE here. One 403 does not separate the '
      + 'location from the account.', pin];
  }
  if (state === 'region-blocked-everywhere') {
    return ['both hosts are refused, so moving this deployment will not help. '
      + "Check the organization's country and any access restriction on the "
      + 'account before touching infrastructure.'];
  }
  if (state === 'credentials-not-geography') {
    return ['not this note. The same key is refused from both hosts, which is a '
      + 'credential question: check that the key exists, is enabled, and belongs '
      + 'to the project you think it does.'];
  }
  if (state === 'credentials-here-only') {
    return ['compare the environment on the two hosts. A key that works from one '
      + 'machine and 401s from another is almost always a different value in the '
      + 'environment rather than a location.'];
  }
  if (state === 'forbidden-unexplained') {
    return ['record the error code exactly as printed and check the provider '
      + 'supported regions list for the country this host egresses from.', pin];
  }
  if (state === 'no-baseline') {
    return ['run this from a host you trust and set LLM_EGRESS_BASELINE to the '
      + 'blob it prints. Without the pair there is one status code and no '
      + 'conclusion.'];
  }
  return [];
}

async function probe(provider, key) {
  const spec = PROVIDERS[provider];
  const url = new URL(spec.url);
  url.searchParams.set('limit', '1');
  const headers = provider === 'openai'
    ? { Authorization: `Bearer ${key}` }
    : { 'x-api-key': key, 'anthropic-version': '2023-06-01' };
  try {
    const r = await fetch(url, { headers });
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    return [r.status, body];
  } catch {
    return [null, null];
  }
}

async function main() {
  const present = Object.keys(PROVIDERS).sort()
    .filter((p) => process.env[PROVIDERS[p].env]);
  if (!present.length) {
    console.error('set OPENAI_API_KEY or ANTHROPIC_API_KEY. Both are used for '
                  + 'one read-only GET of /v1/models and nothing else');
    process.exitCode = 2;
    return;
  }
  const baseline = loadBaseline((process.env.LLM_EGRESS_BASELINE || "dummy-llm-egress-baseline"));
  const observations = [];
  let findings = 0;

  for (const provider of present) {
    const [status, body] = await probe(provider, process.env[PROVIDERS[provider].env]);
    const obs = observation(provider, status, body);
    observations.push(obs);
    const [state, detail] = classify(obs);
    console.log(`${provider.padEnd(11)} ${obs.status ?? '---'}  ${state.padEnd(14)} ${detail}`);

    const [verdict, why] = compare(obs, baseline[provider]);
    console.log(`${verdict.padEnd(20)} ${why}`);
    for (const line of repairLines(verdict)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(verdict)) findings += 1;
  }

  console.log(`baseline: ${blob(observations)}`);
  if (!Object.keys(baseline).length) {
    console.log('no baseline was supplied. Carry that line to the other host and '
                + 'run this again there');
  }
  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
