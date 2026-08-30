/**
 * Audit three closing surfaces for what can still be exported, and by whom.
 *
 * Read only. Every request is a GET: the evals listing, one probe of the
 * prompts path, and one probe per declared prompt id. Nothing here creates an
 * eval, a run or a prompt version.
 *
 * The unit is exportability rather than validity, because what closes on
 * 2026-11-30 is content held on the provider's side. Evals list cleanly,
 * reusable prompts have no documented list endpoint so the path is probed
 * rather than assumed, and Agent Builder has no REST surface at all.
 */
export const API = 'https://api.openai.com/v1';

// Announced 3 June 2026. Published, not readable.
export const SHUTDOWN = '2026-11-30';

export const AGENT_BUILDER = 'agent-builder';

const FINDINGS = new Set(['no-api-surface', 'no-list-endpoint', 'not-readable',
  'not-a-prompt-id', 'malformed', 'credentials', 'refused', 'unreachable',
  'content-to-export']);

const REPAIRS = {
  'no-api-surface':
    'there is no endpoint, so nothing here automates it. Somebody has to open '
    + 'Agent Builder, export each published workflow, and rebuild it with the '
    + 'Agents SDK before the date.',
  'no-list-endpoint':
    'the API reference documents no listing for reusable prompts, so the '
    + 'authoritative roster is a grep of your own tree for pmpt_ ids. Anything '
    + 'only a colleague remembers comes out of the dashboard.',
  'not-readable':
    'nothing answered for this id, so its text is not retrievable by script. '
    + 'Copy it out of the dashboard and put it in the repository before the '
    + 'date, because after it there is nowhere to copy from.',
  'not-a-prompt-id':
    'reusable prompt ids start pmpt_. Fix the configuration; this one was never '
    + 'going to resolve, shutdown or no shutdown.',
  'content-to-export':
    'the listing carries the full definition, so one paginated GET is the whole '
    + 'export. Save it into the repository, then migrate the suites to Promptfoo.',
};

const day = (iso) => Date.parse(`${iso}T00:00:00Z`);

/** Whole days from today to the date. Pure. Negative once it has passed. */
export function daysLeft(today, when = SHUTDOWN) {
  return Math.round((day(String(when)) - day(String(today))) / 86400000);
}

/** How far the API gets on one surface. Pure. [state, detail]. */
export function surfaceReach(name, status) {
  if (String(name) === AGENT_BUILDER) {
    return ['no-api-surface',
      'no documented REST endpoints exist, so nothing here can inventory or export it'];
  }
  if (status === null || status === undefined) {
    return ['unreachable', 'no response at all from this path'];
  }
  const s = Number(status);
  if (s === 200) {
    return ['enumerable', 'the listing answered, so these can be exported by script'];
  }
  if (s === 404) {
    return ['no-list-endpoint',
      'nothing answered at this path, so ids have to come from your own call sites'];
  }
  if (s === 401 || s === 403) {
    return ['credentials', `${s}, so the reach of this surface was not established`];
  }
  return ['refused', `${s}, so the reach of this surface is unknown`];
}

/** Grade one declared prompt id. Pure. Shape first, response second. */
export function promptIdState(pid, status) {
  if (typeof pid !== 'string' || !pid.trim()) {
    return ['malformed',
      'not a usable string, so this is a configuration bug rather than an id'];
  }
  const id = pid.trim();
  if (!id.startsWith('pmpt_')) {
    return ['not-a-prompt-id', 'reusable prompt ids start pmpt_, so this is something else'];
  }
  if (status === null || status === undefined) {
    return ['not-probed', 'no request was made for this id'];
  }
  const s = Number(status);
  if (s === 200) return ['readable', 'the stored content came back'];
  if (s === 404) {
    return ['not-readable',
      'nothing answered, so its text comes out of the dashboard before the date'];
  }
  if (s === 401 || s === 403) return ['credentials', `${s}, which is the key and not the id`];
  return ['refused', `${s}`];
}

/** Turn reach into an owner per surface. Pure. [[name, owner, line]]. */
export function exportPlan(rows) {
  return (rows || []).map(([name, state]) => {
    if (state === 'enumerable') {
      return [name, 'a script', 'one GET per page dumps the full objects'];
    }
    if (state === 'no-list-endpoint') {
      return [name, 'a script, by id', 'probe the ids you hold; the rest is the dashboard'];
    }
    if (state === 'no-api-surface') {
      return [name, 'a person', 'there is no endpoint, so nothing automates this'];
    }
    return [name, 'a person, until proven otherwise',
      'the reach could not be established, so assume the dashboard'];
  });
}

/** The exact GET to run for one export. Pure. Printed, never performed. */
export function exportCommand(kind, ident = null) {
  const auth = '-H "Authorization: Bearer $OPENAI_API_KEY"';
  if (kind === 'evals') return `curl -s ${auth} ${API}/evals?limit=100 > export/evals.json`;
  if (kind === 'prompt') {
    return `curl -s ${auth} ${API}/prompts/${ident} > export/${ident}.json`;
  }
  return '';
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const line = REPAIRS[state];
  if (!line) return [];
  if (state === 'no-list-endpoint' || state === 'not-readable') {
    return [line,
      'then inline it: prompt={id: pmpt_...} becomes an instructions string you '
      + 'hold, which is the short half of this job and the half that is '
      + 'impossible before the export.'];
  }
  return [line];
}

async function getJson(path, key, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(one));
  }
  try {
    const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
    let body = {};
    try { body = await r.json(); } catch { body = {}; }
    return [r.status, body];
  } catch {
    return [null, {}];
  }
}

async function allEvals(key, pages = 50) {
  const out = [];
  let after = null;
  let first = null;
  for (let i = 0; i < pages; i += 1) {
    const params = { limit: 100, order: 'asc' };
    if (after) params.after = after;
    const [status, body] = await getJson('/evals', key, params);
    if (first === null) first = status;
    if (status !== 200) break;
    const page = body.data || [];
    out.push(...page);
    if (!page.length || !body.has_more) break;
    after = page[page.length - 1].id;
    if (!after) break;
  }
  return [first, out];
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project read key. This script only '
                  + 'issues GET requests');
    process.exitCode = 2;
    return;
  }
  const today = (process.env.TODA || "dummy-toda")Y || new Date().toISOString().slice(0, 10);
  const left = daysLeft(today);
  console.log(`three surfaces close ${SHUTDOWN}, ${Math.abs(left)} day(s) `
              + `${left >= 0 ? 'left' : 'past'}`);

  let findings = 0;
  const reach = [];
  const [evalStatus, evals] = await allEvals(key);
  const [promptStatus] = await getJson('/prompts', key, { limit: 1 });

  for (const [name, status] of [['evals', evalStatus], ['prompts', promptStatus],
                                [AGENT_BUILDER, null]]) {
    const [state, detail] = surfaceReach(name, status);
    reach.push([name, state]);
    console.log(`  ${name.padEnd(14)} ${status ?? '---'}  ${state.padEnd(17)} ${detail}`);
    for (const line of repairLines(state)) console.log(`    repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  if (evals.length) {
    console.log(`${evals.length} eval(s) listed, and the listing carries the full definition`);
    console.log(`  ${exportCommand('evals')}`);
    for (const line of repairLines('content-to-export')) console.log(`  repair: ${line}`);
    findings += 1;
  }

  const declared = ((process.env.OPENAI_PROMPT_IDS || "dummy-openai-prompt-ids") ?? '')
    .split(',').map((s) => s.trim()).filter(Boolean);
  if (declared.length) console.log(`${declared.length} declared prompt id(s)`);
  for (const pid of declared) {
    let status = null;
    if (pid.startsWith('pmpt_')) [status] = await getJson(`/prompts/${pid}`, key);
    const [state, detail] = promptIdState(pid, status);
    console.log(`  ${pid.padEnd(12)} ${status ?? '---'}  ${state.padEnd(16)} ${detail}`);
    if (state === 'readable') console.log(`    ${exportCommand('prompt', pid)}`);
    for (const line of repairLines(state)) console.log(`    repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log('plan');
  for (const [name, owner, line] of exportPlan(reach)) {
    console.log(`  ${name.padEnd(14)} ${owner.padEnd(28)} ${line}`);
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
