/**
 * Probe the anthropic-version header three ways, direct and via a gateway.
 *
 * Read only. Every request is a GET of /v1/models, which generates no tokens
 * and bills nothing. Nothing here sends a message, and a 400 from this
 * endpoint costs exactly as little as a 200.
 *
 * No single status is the finding. A required header is only proved required
 * by two probes that disagree about it, and a header injected or stripped in
 * transit is only visible by running the same matrix down two paths.
 */
const API_PATH = '/v1/models';
export const DIRECT = 'https://api.anthropic.com';

export const INITIAL = '2023-01-01';
export const CURRENT = '2023-06-01';
export const KNOWN = [INITIAL, CURRENT];

// The label for the probe that deliberately sends no version header.
export const ABSENT = '(absent)';

const FINDINGS = new Set(['version-not-enforced', 'current-rejected',
  'ancient-pinned', 'unknown-version-pinned', 'gateway-injects',
  'gateway-strips', 'gateway-disagrees', 'unreachable']);

const REPAIRS = {
  'version-not-enforced':
    'something on this path adds anthropic-version for you. Find it, then set '
    + 'the header in each client as well: a header the infrastructure supplies '
    + 'is a header your code does not have.',
  'gateway-injects':
    'set anthropic-version: 2023-06-01 in the client itself. A client that only '
    + 'works behind the gateway is one routing change from a 400 on every '
    + 'request.',
  'gateway-strips':
    'the gateway is removing or rewriting anthropic-version. Fix it there; a '
    + 'client cannot compensate for a header that does not survive the hop.',
  'gateway-disagrees':
    'the two paths do not behave the same. Read the gateway request header '
    + 'policy before trusting either matrix as a description of your clients.',
  'current-rejected':
    'the current version probe did not return 200, so this is a credential or '
    + 'connectivity problem rather than a versioning one. Nothing else in this '
    + 'matrix can be trusted until it is.',
  'ancient-pinned':
    'move the pin to anthropic-version: 2023-06-01, and read your streaming '
    + 'code first: 2023-06-01 sends incremental named events and no '
    + 'data: [DONE].',
  'unknown-version-pinned':
    'only 2023-01-01 and 2023-06-01 have ever existed. Replace the string with '
    + '2023-06-01 rather than trying to make it work.',
};

/** The version header for one probe. Pure. Empty object for ABSENT. */
export function probeHeaders(label) {
  if (label === ABSENT) return {};
  return { 'anthropic-version': String(label).trim() };
}

/** The ordered probe set. Pure. ABSENT, the two real versions, then yours. */
export function probeLabels(declared) {
  const out = [ABSENT, CURRENT, INITIAL];
  for (const raw of declared ?? []) {
    const text = String(raw ?? '').trim();
    if (text && !out.includes(text)) out.push(text);
  }
  return out;
}

/** What one probe result means on its own. Pure. Returns [state, detail]. */
export function classifyStatus(label, status) {
  if (status === null || status === undefined) {
    return ['unreachable', 'no response at all from this host'];
  }
  const code = Math.trunc(Number(status));
  if (label === ABSENT) {
    if (code === 400) return ['enforced', '400 with no version header, which is correct'];
    if (code === 200) {
      return ['not-enforced',
        '200 with no version header, so something on this path is supplying '
        + 'one for you'];
    }
    if (code === 401 || code === 403) {
      return ['credentials',
        `${code}, so this probe says nothing about the version header`];
    }
    return ['unexpected', `${code} with no version header`];
  }
  if (code === 200) {
    if (label === CURRENT) return ['accepted', '200, the current version'];
    if (label === INITIAL) {
      return ['accepted-deprecated',
        '200, but 2023-01-01 is deprecated and predates the named SSE events'];
    }
    return ['accepted-unknown',
      '200 for a string that is not one of the two documented versions'];
  }
  if (code === 401 || code === 403) {
    return ['credentials', `${code}, which is the credential rather than the version`];
  }
  if (code === 400 || code === 404 || code === 410) {
    return ['refused', `${code}, this host will not serve that version`];
  }
  return ['unexpected', `${code}`];
}

/** Grade one host's whole matrix. Pure. Returns [state, detail]. */
export function hostVerdict(results) {
  const r = { ...(results ?? {}) };
  const current = r[CURRENT];
  const absent = r[ABSENT];
  if (current === null || current === undefined) {
    return ['unreachable',
      'the current version probe got no response, so nothing else on this host '
      + 'can be read'];
  }
  const code = Math.trunc(Number(current));
  if (code === 401 || code === 403) {
    return ['current-rejected',
      `${code} for anthropic-version: ${CURRENT}, which is a credential problem `
      + 'and not a versioning one'];
  }
  if (code !== 200) {
    return ['current-rejected',
      `${code} for anthropic-version: ${CURRENT}, which should be 200`];
  }
  if (absent !== null && absent !== undefined && Math.trunc(Number(absent)) === 200) {
    return ['version-not-enforced',
      '200 with no anthropic-version header at all. The header is documented as '
      + 'required, so a proxy, SDK or gateway on this path is adding it'];
  }
  return ['version-enforced',
    `400 without the header and 200 with ${CURRENT}, which is the shape a `
    + 'direct connection should have'];
}

/** [[version, state, detail]] for the strings your clients send. Pure. */
export function declaredFindings(results, declared) {
  const r = { ...(results ?? {}) };
  const seen = new Set();
  const out = [];
  for (const raw of declared ?? []) {
    const text = String(raw ?? '').trim();
    if (!text || seen.has(text) || text === CURRENT) continue;
    seen.add(text);
    const status = r[text];
    const suffix = (status === null || status === undefined)
      ? '' : ` (this host returns ${Math.trunc(Number(status))} for it)`;
    if (text === INITIAL) {
      out.push([text, 'ancient-pinned',
        '2023-01-01 is the initial release and is deprecated. A client pinned '
        + 'there does not get the 2023-06-01 SSE format: incremental named '
        + 'events, and no data: [DONE]' + suffix]);
    } else {
      out.push([text, 'unknown-version-pinned',
        'only 2023-01-01 and 2023-06-01 have ever existed, so this string is a '
        + 'typo or an invention' + suffix]);
    }
  }
  out.sort((a, b) => a[0].localeCompare(b[0]));
  return out;
}

/** Compare two hosts' matrices. Pure. Returns [state, detail]. */
export function gatewayVerdict(direct, proxy) {
  const d = { ...(direct ?? {}) };
  const p = { ...(proxy ?? {}) };
  if (Object.keys(p).length === 0) {
    return ['no-gateway',
      'no gateway base URL was given, so nothing was compared. A header added '
      + 'in transit is invisible to a single host'];
  }
  const num = (v) => ((v === null || v === undefined) ? null : Math.trunc(Number(v)));
  const dAbsent = num(d[ABSENT]);
  const pAbsent = num(p[ABSENT]);
  const dCurrent = num(d[CURRENT]);
  const pCurrent = num(p[CURRENT]);
  if (dAbsent === 400 && pAbsent === 200) {
    return ['gateway-injects',
      'the direct host 400s without the header and the gateway returns 200, so '
      + 'the gateway adds anthropic-version for you. Every client behind it is '
      + 'untested'];
  }
  if (dCurrent === 200 && pCurrent !== null && pCurrent !== 200) {
    return ['gateway-strips',
      `anthropic-version: ${CURRENT} is accepted directly and returns ${pCurrent} `
      + 'through the gateway, so it is being stripped or rewritten in transit'];
  }
  const labels = [...new Set([...Object.keys(d), ...Object.keys(p)])].sort();
  const differing = labels.filter((l) => (d[l] ?? null) !== (p[l] ?? null));
  if (differing.length) {
    return ['gateway-disagrees',
      'the two hosts return different statuses for: ' + differing.join(', ')];
  }
  return ['gateway-agrees',
    'both hosts return the same status for every probe, so nothing on the way '
    + 'is rewriting the header'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const line = REPAIRS[state];
  if (!line) return [];
  if (state === 'gateway-injects' || state === 'gateway-strips'
      || state === 'version-not-enforced') {
    return [line,
      'the durable fix is the official SDK, which sets anthropic-version on '
      + 'every request whether or not anything else does.'];
  }
  return [line];
}

async function probe(base, key, label) {
  const url = new URL(base.replace(/\/+$/, '') + API_PATH);
  url.searchParams.set('limit', '1');
  try {
    const r = await fetch(url, { headers: { 'x-api-key': key, ...probeHeaders(label) } });
    return r.status;
  } catch {
    return null;
  }
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key. This script only '
                  + 'issues GET requests against /v1/models');
    process.exitCode = 2;
    return;
  }
  const declared = ((process.env.ANTHROPIC_VERSIONS || "dummy-anthropic-versions") ?? '')
    .split(',').map((s) => s.trim()).filter(Boolean);
  const labels = probeLabels(declared);

  const gateway = (process.env.ANTHROPIC_BASE_URL || "https://example.com");
  const hosts = [['direct', DIRECT]];
  if (gateway && gateway.replace(/\/+$/, '') !== DIRECT) hosts.push(['gateway', gateway]);

  const matrices = {};
  let findings = 0;

  for (const [role, base] of hosts) {
    const results = {};
    console.log(`host ${base}`);
    for (const label of labels) {
      const status = await probe(base, key, label);
      results[label] = status;
      const [state, detail] = classifyStatus(label, status);
      console.log(`  ${label.padEnd(13)} ${status ?? '---'}  ${state.padEnd(20)} ${detail}`);
    }
    matrices[role] = results;
    const [state, detail] = hostVerdict(results);
    console.log(`${state.padEnd(20)} ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  const [gstate, gdetail] = gatewayVerdict(matrices.direct, matrices.gateway);
  console.log(`${gstate.padEnd(20)} ${gdetail}`);
  for (const line of repairLines(gstate)) console.log(`  repair: ${line}`);
  if (FINDINGS.has(gstate)) findings += 1;

  for (const [version, state, detail] of declaredFindings(matrices.direct, declared)) {
    console.log(`${state.padEnd(20)} ${version}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
