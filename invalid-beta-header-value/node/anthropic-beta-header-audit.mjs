/**
 * Grade every anthropic-beta string your code sends, without sending one.
 *
 * Read only. Every request is a GET: /v1/models to validate a name, and the
 * two readable listings twice each to compare a response with and without a
 * header. No request body is constructed and nothing is generated or billed.
 *
 * A 200 proves the name is recognised by the request layer. It is not evidence
 * that the beta still does anything on /v1/messages, and nothing here says so.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// A dictionary for near-matching a rejected string, never a verdict. The probe
// is the authority; a document lags.
export const KNOWN_BETAS = [
  'message-batches-2024-09-24', 'prompt-caching-2024-07-31',
  'computer-use-2024-10-22', 'computer-use-2025-01-24',
  'computer-use-2025-11-24', 'pdfs-2024-09-25',
  'token-counting-2024-11-01', 'token-efficient-tools-2025-02-19',
  'output-128k-2025-02-19', 'output-300k-2026-03-24',
  'files-api-2025-04-14', 'mcp-client-2025-04-04', 'mcp-client-2025-11-20',
  'mcp-tunnels-2026-06-22', 'dev-full-thinking-2025-05-14',
  'interleaved-thinking-2025-05-14', 'code-execution-2025-05-22',
  'extended-cache-ttl-2025-04-11', 'context-1m-2025-08-07',
  'context-management-2025-06-27',
  'model-context-window-exceeded-2025-08-26', 'skills-2025-10-02',
  'fast-mode-2026-02-01', 'user-profiles-2026-03-24',
  'user-profiles-2026-08-18', 'advisor-tool-2026-03-01',
  'managed-agents-2026-04-01', 'agent-memory-2026-07-22',
  'cache-diagnosis-2026-04-07', 'dreaming-2026-04-21',
  'thinking-token-count-2026-05-13', 'thinking-display-updates-2026-08-18',
  'server-side-fallback-2026-06-01', 'server-side-fallback-2026-07-01',
  'fallback-credit-2026-06-01', 'fallback-credit-2026-07-01',
  'mid-conversation-tool-changes-2026-07-01', 'compact-2026-01-12',
  'structured-outputs-2025-11-13', 'task-budgets-2026-03-13',
  'ce-user-management-2026-07-13',
];

// On memory store endpoints the first replaces the second; both is a 400.
export const CONFLICTS = [['agent-memory-2026-07-22', 'managed-agents-2026-04-01']];

const DIFF_PATHS = ['/models', '/files'];

const FINDINGS = new Set(['rejected-typo', 'rejected-unknown',
  'pinned-to-beta-shape', 'conflicting-pair', 'malformed-header']);

/** [names, faults] from one anthropic-beta header value. Pure. */
export function splitBetas(raw) {
  const names = [];
  const faults = [];
  const seen = new Set();
  const text = String(raw ?? '');
  for (const segment of text.split(',')) {
    let piece = segment.trim();
    if (!piece) {
      if (segment || text.includes(',')) {
        faults.push('an empty segment, usually a trailing comma');
      }
      continue;
    }
    if (piece !== piece.toLowerCase()) {
      faults.push(`'${piece}' is not lower case; beta names are exact`);
      piece = piece.toLowerCase();
    }
    if (piece.includes(' ') || piece.includes('\t')) {
      faults.push(`'${piece}' contains whitespace inside the name`);
    }
    if (seen.has(piece)) {
      faults.push(`'${piece}' is listed more than once`);
      continue;
    }
    seen.add(piece);
    names.push(piece);
  }
  return [names, [...new Set(faults)]];
}

/** {call site: raw header value}. Pure. Accepts JSON, a list or a string. */
export function loadCallSites(raw) {
  const text = String(raw ?? '').trim();
  if (!text) return {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { '(declared)': text };
  }
  if (Array.isArray(parsed)) return { '(declared)': parsed.map(String).join(',') };
  if (parsed && typeof parsed === 'object') {
    return Object.fromEntries(Object.entries(parsed).map(([k, v]) => [String(k), String(v)]));
  }
  return { '(declared)': String(parsed) };
}

/** Edit distance between two strings. Pure. Written out to match the Python. */
export function levenshtein(a, b) {
  const x = String(a ?? '');
  const y = String(b ?? '');
  if (x === y) return 0;
  if (!x.length) return y.length;
  if (!y.length) return x.length;
  let previous = Array.from({ length: y.length + 1 }, (_, i) => i);
  for (let i = 1; i <= x.length; i += 1) {
    const current = [i];
    for (let j = 1; j <= y.length; j += 1) {
      current.push(Math.min(previous[j] + 1, current[j - 1] + 1,
                            previous[j - 1] + (x[i - 1] === y[j - 1] ? 0 : 1)));
    }
    previous = current;
  }
  return previous[y.length];
}

/** The closest documented names to a rejected string. Pure. */
export function nearMatches(name, known = KNOWN_BETAS, limit = 3, maxDistance = 6) {
  const scored = [];
  for (const candidate of known ?? []) {
    const distance = levenshtein(name, candidate);
    if (distance <= maxDistance) scored.push([distance, candidate]);
  }
  scored.sort((a, b) => (a[0] - b[0]) || a[1].localeCompare(b[1]));
  return scored.slice(0, limit).map(([, candidate]) => candidate);
}

/** What one probe of one beta name means. Pure. Returns [state, detail]. */
export function classifyProbe(name, status, known = KNOWN_BETAS) {
  if (status === null || status === undefined) {
    return ['unreachable', 'no response, so this name was not graded'];
  }
  const code = Math.trunc(Number(status));
  const documented = (known ?? []).includes(name);
  if (code === 200) {
    if (documented) return ['accepted', '200, and the published enum lists it'];
    return ['accepted-undocumented',
      '200, but the published enum does not list it. The endpoint accepts it, '
      + 'so the list is behind rather than the header being wrong'];
  }
  if (code === 400) {
    return [nearMatches(name, known).length ? 'rejected-typo' : 'rejected-unknown',
      '400. Invalid, or a beta this organization is not entitled to; the API '
      + 'returns the same message for both'];
  }
  if (code === 401 || code === 403) {
    return ['credentials', `${code}, which is the key rather than the beta name`];
  }
  return ['unexpected', `${code}`];
}

/** [top-level keys, keys on the first data item]. Pure. */
export function keySets(payload) {
  const body = (payload && typeof payload === 'object' && !Array.isArray(payload))
    ? payload : {};
  const top = Object.keys(body).map(String).sort();
  const data = body.data;
  const first = Array.isArray(data) && data.length ? data[0] : null;
  const item = (first && typeof first === 'object')
    ? Object.keys(first).map(String).sort() : [];
  return [top, item];
}

/** Which keys differ between two bodies. Pure. */
export function shapeDelta(withHeader, withoutHeader) {
  const [wTop, wItem] = keySets(withHeader);
  const [nTop, nItem] = keySets(withoutHeader);
  const only = (a, b) => a.filter((k) => !b.includes(k)).sort();
  return {
    top: [only(wTop, nTop), only(nTop, wTop)],
    item: [only(wItem, nItem), only(nItem, wItem)],
  };
}

/** Grade one accepted name by response shape. Pure. Returns [state, detail]. */
export function graduationVerdict(name, deltas) {
  const changed = [];
  for (const path of Object.keys(deltas ?? {}).sort()) {
    const delta = (deltas ?? {})[path] ?? {};
    const groups = [delta.top ?? [[], []], delta.item ?? [[], []]];
    if (groups.some((g) => g.some((side) => (side ?? []).length))) changed.push(path);
  }
  if (changed.length) {
    return ['pinned-to-beta-shape',
      'accepted, and the response differs with and without it on: ' + changed.join(', ')];
  }
  return ['no-visible-difference',
    'same keys with and without it on the endpoints this script can read, which '
    + 'is not evidence that the header does nothing'];
}

/** [[a, b]] documented pairs present together. Pure. */
export function conflicting(names) {
  const have = new Set((names ?? []).map((n) => String(n).trim().toLowerCase()));
  return CONFLICTS.filter((pair) => pair.every((n) => have.has(n)));
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, name = null, matches = [], deltas = null) {
  if (state === 'rejected-typo') {
    return [`replace it with ${matches[0] ?? 'the documented name'}, then re-run this probe.`,
      'if the spelling is already exact, the other cause is entitlement: the '
      + 'same 400 is returned for a beta this organization does not have access to.'];
  }
  if (state === 'rejected-unknown') {
    return [`nothing in the published enum is close to '${name}'. Read the beta `
      + 'headers reference for the current name, and check entitlement before '
      + 'assuming it is a typo.'];
  }
  if (state === 'pinned-to-beta-shape') {
    const lines = ['the beta graduated. The header is optional now and it is not '
      + 'inert: it holds this client on the response shape it shipped with. Read '
      + 'the migration notes before dropping it.'];
    for (const path of Object.keys(deltas ?? {}).sort()) {
      const delta = (deltas ?? {})[path] ?? {};
      const [onlyWith, onlyWithout] = delta.top ?? [[], []];
      if (onlyWith.length) lines.push(`${path} top-level keys only with the header: ${onlyWith.join(', ')}`);
      if (onlyWithout.length) lines.push(`${path} top-level keys only without it: ${onlyWithout.join(', ')}`);
      const [iWith, iWithout] = delta.item ?? [[], []];
      if (iWith.length) lines.push(`${path} item keys only with the header: ${iWith.join(', ')}`);
      if (iWithout.length) lines.push(`${path} item keys only without it: ${iWithout.join(', ')}`);
    }
    return lines;
  }
  if (state === 'conflicting-pair') {
    return ['on memory store endpoints the first replaces the second. Sending '
      + 'both returns 400. Send agent-memory-2026-07-22 alone there and keep '
      + 'managed-agents-2026-04-01 for the agent, session and environment endpoints.'];
  }
  if (state === 'malformed-header') {
    return ['multiple betas go in one comma separated header. Rebuild the string '
      + 'from a list rather than concatenating, and note that repeating a --beta '
      + 'flag on the CLI keeps only the first.'];
  }
  return [];
}

async function read(key, path, beta, params = { limit: 1 }) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const headers = { 'x-api-key': key, 'anthropic-version': VERSION };
  if (beta) headers['anthropic-beta'] = beta;
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
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key. This script only '
                  + 'issues GET requests');
    process.exitCode = 2;
    return;
  }
  const callSites = loadCallSites((process.env.ANTHROPIC_BETA_HEADERS || "dummy-anthropic-beta-headers"));
  if (!Object.keys(callSites).length) {
    console.error('nothing to grade. Set ANTHROPIC_BETA_HEADERS to a JSON map of '
                  + 'call site to header value');
    process.exitCode = 2;
    return;
  }

  let findings = 0;
  const distinct = [];
  for (const site of Object.keys(callSites).sort()) {
    const [names, faults] = splitBetas(callSites[site]);
    for (const name of names) if (!distinct.includes(name)) distinct.push(name);
    for (const fault of faults) {
      console.log(`${'malformed-header'.padEnd(20)} ${site} sends ${fault}`);
      findings += 1;
    }
    if (faults.length) {
      for (const line of repairLines('malformed-header')) console.log(`  repair: ${line}`);
    }
    for (const pair of conflicting(names)) {
      console.log(`${'conflicting-pair'.padEnd(20)} ${site} sends ${pair[0]} with ${pair[1]}`);
      for (const line of repairLines('conflicting-pair')) console.log(`  repair: ${line}`);
      findings += 1;
    }
  }

  console.log(`${distinct.length} distinct beta string(s) across `
              + `${Object.keys(callSites).length} call site(s)`);

  for (const name of distinct) {
    const [status] = await read(key, '/models', name);
    const [state, detail] = classifyProbe(name, status);
    console.log(`${state.padEnd(20)} ${name}: ${detail}`);
    const matches = state.startsWith('rejected') ? nearMatches(name) : [];
    if (matches.length) console.log(`  closest documented names: ${matches.join(', ')}`);
    for (const line of repairLines(state, name, matches)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) { findings += 1; continue; }
    if (state !== 'accepted' && state !== 'accepted-undocumented') continue;

    const deltas = {};
    for (const path of DIFF_PATHS) {
      const [withStatus, withBody] = await read(key, path, name);
      const [withoutStatus, withoutBody] = await read(key, path, null);
      if (withStatus !== 200 || withoutStatus !== 200) continue;
      deltas[path] = shapeDelta(withBody, withoutBody);
    }
    if (!Object.keys(deltas).length) {
      console.log('  neither listing was readable, so no shape comparison was '
                  + 'made for this name');
      continue;
    }
    const [gstate, gdetail] = graduationVerdict(name, deltas);
    console.log(`${gstate.padEnd(20)} ${name}: ${gdetail}`);
    for (const line of repairLines(gstate, name, [], deltas)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(gstate)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
