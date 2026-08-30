/**
 * Find OpenAI tool definitions that are sent on every call and never chosen.
 *
 * Read only. One GET per stored response id, using a project key. No
 * completion is created: /v1/responses is read, never posted to.
 *
 * There is no list endpoint for stored responses, so the sample comes from a
 * file of ids you supply, and every claim is bounded by that sample.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

// Output items that represent the model choosing one of your function tools.
const CALL_TYPES = new Set(['function_call', 'custom_tool_call']);

const CROWD_CEILING = 20;

const FINDINGS = new Set(['never-called', 'never-offered']);

/** Read a count as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Response ids out of a plain text file. Pure. Order kept, duplicates dropped.
 * Also the guard that stops an arbitrary line becoming a URL path segment.
 */
export function parseIds(text) {
  const out = [];
  const seen = new Set();
  for (const line of String(text ?? '').split('\n')) {
    const candidate = line.split('#')[0].trim();
    if (!candidate || !candidate.startsWith('resp_')) continue;
    if (!/^[A-Za-z0-9_-]+$/.test(candidate)) continue;
    if (seen.has(candidate)) continue;
    seen.add(candidate);
    out.push(candidate);
  }
  return out;
}

/**
 * The function name out of either tool shape. Pure. Null when absent.
 * Responses puts name at the top level; Chat Completions nests it under
 * function, and a reader that knows one shape is blind on half a corpus.
 */
export function toolName(tool) {
  if (!tool || typeof tool !== 'object') return null;
  let name = tool.name;
  if (!name && tool.function && typeof tool.function === 'object') {
    name = tool.function.name;
  }
  const text = String(name ?? '').trim();
  return text || null;
}

/** Every named tool the request declared, with its size in characters. Pure. */
export function declaredTools(response) {
  const out = {};
  for (const tool of response?.tools ?? []) {
    const name = toolName(tool);
    if (name === null) continue;
    let size = 0;
    try {
      size = JSON.stringify(tool, Object.keys(tool).sort()).length;
    } catch {
      size = 0;
    }
    out[name] = Math.max(out[name] ?? 0, size);
  }
  return out;
}

/** Tool names the model actually chose in one response, counted. Pure. */
export function calledTools(response) {
  const counts = {};
  for (const item of response?.output ?? []) {
    if (!item || typeof item !== 'object' || !CALL_TYPES.has(item.type)) continue;
    const name = String(item.name ?? '').trim();
    if (!name) continue;
    counts[name] = (counts[name] ?? 0) + 1;
  }
  return counts;
}

/**
 * How free the model was to pick a tool in this turn. Pure.
 * "free", "blocked", or "named:<tool>". Absent tool_choice is auto, which is
 * free, and that is the line between a tool ignored and a tool ruled out.
 */
export function choiceMode(response) {
  const choice = response?.tool_choice;
  if (choice === null || choice === undefined) return 'free';
  if (typeof choice === 'string') {
    return choice.trim().toLowerCase() === 'none' ? 'blocked' : 'free';
  }
  if (typeof choice === 'object') {
    const name = toolName(choice);
    return name ? `named:${name}` : 'free';
  }
  return 'free';
}

/**
 * Fold a sample of stored responses into one corpus. Pure.
 * Declarations and offers are counted separately: a tool ruled out by
 * tool_choice on every turn is not dead weight and needs a different repair.
 */
export function fold(responses) {
  const corpus = { sampled: 0, withTools: 0, widestTurn: 0, calls: 0,
                   declared: {}, offered: {}, called: {} };
  for (const response of responses ?? []) {
    if (!response || typeof response !== 'object') continue;
    corpus.sampled += 1;
    const declared = declaredTools(response);
    for (const [name, count] of Object.entries(calledTools(response))) {
      corpus.called[name] = (corpus.called[name] ?? 0) + count;
      corpus.calls += count;
    }
    const names = Object.keys(declared);
    if (names.length === 0) continue;
    corpus.withTools += 1;
    corpus.widestTurn = Math.max(corpus.widestTurn, names.length);
    const mode = choiceMode(response);
    for (const name of names) {
      const row = corpus.declared[name] ?? { turns: 0, chars: 0 };
      row.turns += 1;
      row.chars = Math.max(row.chars, declared[name]);
      corpus.declared[name] = row;
      if (mode === 'blocked') continue;
      if (mode.startsWith('named:') && mode.slice('named:'.length) !== name) continue;
      corpus.offered[name] = (corpus.offered[name] ?? 0) + 1;
    }
  }
  return corpus;
}

/** One row per declared tool. Pure. Least used and most expensive first. */
export function coverage(corpus) {
  const rows = [];
  for (const [name, row] of Object.entries(corpus?.declared ?? {})) {
    rows.push({ name,
                turns: readInt(row?.turns),
                chars: readInt(row?.chars),
                offered: readInt(corpus?.offered?.[name]),
                calls: readInt(corpus?.called?.[name]) });
  }
  rows.sort((a, b) => (a.calls - b.calls) || (b.chars - a.chars)
    || a.name.localeCompare(b.name));
  return rows;
}

/** Names the model called that no sampled request declared. Pure. */
export function orphanCalls(corpus) {
  const declared = new Set(Object.keys(corpus?.declared ?? {}));
  return Object.keys(corpus?.called ?? {}).filter((n) => !declared.has(n)).sort();
}

/** Classify one tool's coverage across the sample. Pure. Returns [state, detail]. */
export function classify(row, minOffered = 50, rare = 0.01) {
  const name = String(row?.name ?? 'unknown');
  const turns = readInt(row?.turns);
  const offered = readInt(row?.offered);
  const calls = readInt(row?.calls);

  if (turns > 0 && offered === 0) {
    return ['never-offered',
      `declared in ${turns} turn(s), free to be chosen in 0 of them. ` +
      'tool_choice ruled it out every time, so the model never declined it ' +
      'and rewriting the description changes nothing.'];
  }
  if (offered < minOffered) {
    return ['too-small-a-sample',
      `offered in ${offered} turn(s), under the floor of ${minOffered}. ` +
      'Not enough to call anything dead.'];
  }
  if (calls === 0) {
    return ['never-called',
      `offered in ${offered} of ${turns} turn(s), called 0 time(s), ` +
      `${readInt(row?.chars)} schema char(s). Sent and billed on every one ` +
      'of those turns.'];
  }
  const share = calls / offered;
  if (share < rare) {
    return ['rarely-called',
      `offered in ${offered} turn(s), called ${calls} time(s) ` +
      `(${(share * 100).toFixed(1)}%). Worth keeping and worth not sending ` +
      `on every turn. ${name} is the exception, not the default.`];
  }
  return ['called',
    `offered in ${offered} turn(s), called ${calls} time(s) ` +
    `(${(share * 100).toFixed(1)}%).`];
}

/**
 * Share of the declared schema, in characters, that nothing ever calls. Pure.
 * Characters, never tokens. The token price is measured exactly and for free
 * elsewhere, and a character count dressed as a token count is worse than none.
 */
export function deadWeight(rows, minOffered = 50, rare = 0.01) {
  let total = 0;
  let dead = 0;
  for (const row of rows ?? []) {
    const chars = readInt(row?.chars);
    total += chars;
    if (classify(row, minOffered, rare)[0] === 'never-called') dead += chars;
  }
  if (total <= 0) return null;
  return dead / total;
}

/** What the widest turn in the sample looked like. Pure. */
export function crowding(widestTurn, ceiling = CROWD_CEILING) {
  const widest = readInt(widestTurn);
  if (widest <= 0) return ['no-tools', 'no sampled response declared any named tool'];
  if (widest > ceiling) {
    return ['crowded',
      `the widest turn offered ${widest} tools, above the guidance of fewer ` +
      `than ${ceiling}. Selection quality falls with crowding and it falls on ` +
      'the vaguest descriptions first.'];
  }
  return ['within-guidance',
    `the widest turn offered ${widest} tool(s), inside the guidance of fewer ` +
    `than ${ceiling}`];
}

/** The repair for one classified tool. Pure. */
export function repairLines(state, name) {
  if (state === 'never-called') {
    return [
      `the description probably reads like a signature. Rewrite it as a ` +
      `selection rule: when to call ${name}, and when not to.`,
      'if a call is mandatory, say so with tool_choice required or a named ' +
      'tool rather than hoping the model picks it up.',
      'if nothing needs it, delete it. It is billed on every turn.',
    ];
  }
  if (state === 'never-offered') {
    return [`tool_choice never let the model near ${name}. Fix the request ` +
            'before you touch the description.'];
  }
  if (state === 'rarely-called') {
    return [`keep ${name}, but stop sending it on every turn. allowed_tools ` +
            'narrows the set for the turns where it is plausible.'];
  }
  return [];
}

async function get(key, path) {
  const res = await fetch(API + path, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: OPENAI_API_KEY needs read ` +
                    'access to stored responses in this project');
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key that can read stored responses');
    process.exitCode = 2;
    return;
  }
  const file = process.argv.slice(2).find((a) => !a.startsWith('--'));
  if (!file) {
    console.error('pass a text file of stored response ids, one per line');
    process.exitCode = 2;
    return;
  }
  const minOffered = Number((process.env.MIN_OFFERED || "dummy-min-offered") ?? 50);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const ids = parseIds(await readFile(file, 'utf8'));
  if (ids.length === 0) {
    console.error('no usable response ids. /v1/responses cannot be listed, so ' +
                  'the sample has to come from your own request log');
    process.exitCode = 2;
    return;
  }

  const responses = [];
  let missing = 0;
  for (const id of ids) {
    const body = await get(key, `/responses/${id}`);
    if (body === null) missing += 1;
    else responses.push(body);
  }
  if (missing > 0) {
    console.log(`${missing} of ${ids.length} id(s) no longer resolve; stored ` +
                'responses are not kept forever');
  }

  const corpus = fold(responses);
  const rows = coverage(corpus);
  if (rows.length === 0) {
    console.log(`no named tools declared in ${corpus.sampled} sampled response(s)`);
    return;
  }

  const orphans = orphanCalls(corpus);
  if (orphans.length > 0) {
    console.warn(`called but never declared in this sample: ${orphans.join(', ')}. ` +
                 'The sample mixes two configurations, so the set difference ' +
                 'below is not reliable.');
  }

  let bad = 0;
  for (const row of rows) {
    const [state, detail] = classify(row, minOffered);
    const line = `${state.padEnd(19)} ${row.name.padEnd(22)} ${detail}`;
    if (FINDINGS.has(state) || state === 'rarely-called') {
      if (FINDINGS.has(state)) bad += 1;
      console.warn(line);
      for (const repair of repairLines(state, row.name)) {
        console.warn(`  repair: ${repair}`);
      }
    } else if (showAll || state === 'too-small-a-sample') {
      console.log(line);
    }
  }

  console.log(`${rows.length} declared tool(s) over ${corpus.sampled} ` +
              `response(s), ${bad} finding(s)`);

  const share = deadWeight(rows, minOffered);
  if (share !== null) {
    console.log(`${(share * 100).toFixed(0)}% of the declared schema, in ` +
                'characters, belongs to tools nothing ever called. Characters ' +
                'are not tokens: count the block for free against count_tokens ' +
                'before pricing it.');
  }

  const [state, detail] = crowding(corpus.widestTurn);
  if (state === 'crowded') {
    console.warn(`${state.padEnd(19)} ${detail}`);
    console.warn('  repair: narrow the turn with allowed_tools rather than ' +
                 'rewriting one description at a time.');
  } else {
    console.log(`${state.padEnd(19)} ${detail}`);
  }

  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
