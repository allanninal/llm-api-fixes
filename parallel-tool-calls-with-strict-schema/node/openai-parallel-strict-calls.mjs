/**
 * Find OpenAI turns where parallel tool calls voided a strict schema.
 *
 * Read only. One GET per stored response id, using a project key. No
 * completion is created: /v1/responses is read, never posted to.
 *
 * Structured Outputs is not supported alongside parallel function calls, and
 * parallel_tool_calls defaults to true. A turn returning more than one
 * function_call item while any tool declares strict came back without the
 * guarantee the parser relies on, and it did so with an HTTP 200.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

const CALL_TYPES = new Set(['function_call', 'custom_tool_call']);

const FINDINGS = new Set(['strict-void']);

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

/** The function name out of either tool shape. Pure. Null when absent. */
export function toolName(tool) {
  if (!tool || typeof tool !== 'object') return null;
  let name = tool.name;
  if (!name && tool.function && typeof tool.function === 'object') {
    name = tool.function.name;
  }
  const text = String(name ?? '').trim();
  return text || null;
}

/** Every named tool the request declared. Pure. Sorted. */
export function declaredNames(response) {
  const out = new Set();
  for (const tool of response?.tools ?? []) {
    const name = toolName(tool);
    if (name) out.add(name);
  }
  return [...out].sort();
}

/**
 * Tools declaring strict true, in either shape. Pure. Sorted.
 * strict false and strict absent are the same thing here and neither counts.
 */
export function strictTools(response) {
  const out = new Set();
  for (const tool of response?.tools ?? []) {
    if (!tool || typeof tool !== 'object') continue;
    let strict = tool.strict;
    if (strict !== true && tool.function && typeof tool.function === 'object') {
      strict = tool.function.strict;
    }
    if (strict !== true) continue;
    const name = toolName(tool);
    if (name) out.add(name);
  }
  return [...out].sort();
}

/**
 * Could the model return more than one tool call in this turn? Pure.
 * An absent parallel_tool_calls is true, and reading it as false is the exact
 * mistake that makes this whole class of failure invisible.
 */
export function parallelAllowed(response) {
  return response?.parallel_tool_calls !== false;
}

/** The tool calls in one turn, in order. Pure. call_id is kept deliberately. */
export function functionCalls(response) {
  const out = [];
  for (const item of response?.output ?? []) {
    if (!item || typeof item !== 'object' || !CALL_TYPES.has(item.type)) continue;
    const name = String(item.name ?? '').trim();
    if (!name) continue;
    out.push({ name, callId: String(item.call_id ?? '') });
  }
  return out;
}

/** Tool names called more than once in one turn. Pure. */
export function duplicateNames(calls) {
  const counts = {};
  for (const call of calls ?? []) {
    const name = String(call?.name ?? '');
    if (name) counts[name] = (counts[name] ?? 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).filter(([, n]) => n > 1));
}

/** Classify one turn. Pure. Returns [state, detail]. The unit is the turn. */
export function classify(response) {
  const declared = declaredNames(response);
  if (declared.length === 0) return ['no-tools', 'no named tools declared in this turn'];

  const strict = strictTools(response);
  const calls = functionCalls(response);
  const parallel = parallelAllowed(response);
  const names = calls.map((c) => c.name).join(', ') || 'none';

  if (strict.length === 0) {
    if (calls.length > 1) {
      return ['fanout-no-strict',
        `${calls.length} function_call item(s) in one turn (${names}) and no ` +
        'tool declares strict. There was no guarantee to void here: the ' +
        'arguments were never validated by the API at all, which is a ' +
        'different fault with a different repair.'];
    }
    return ['no-strict-declared',
      `${declared.length} tool(s) declared, none of them strict. Nothing in ` +
      'this turn was schema-guaranteed.'];
  }

  if (!parallel) {
    return ['strict-serialised',
      `strict declared on ${strict.length} tool(s) and parallel_tool_calls is ` +
      'false. The guarantee holds.'];
  }

  if (calls.length > 1) {
    return ['strict-void',
      `${calls.length} function_call item(s) in one turn with strict declared ` +
      `and parallel_tool_calls left on (${names}). Structured Outputs is not ` +
      'supported alongside parallel calls, so these argument objects carry no ' +
      'schema guarantee.'];
  }

  return ['strict-at-risk',
    `strict declared on ${strict.length} tool(s) with parallel_tool_calls left ` +
    `on, and this turn happened to return ${calls.length} call(s). The ` +
    'configuration is loaded; it did not fire here.'];
}

/**
 * How often the fan-out that voids the guarantee actually happens. Pure.
 * The denominator is turns that were at risk, never all turns, and it is null
 * when nothing was at risk rather than a number invented over zero.
 */
export function exposure(states) {
  const list = states ?? [];
  const atRisk = list.filter((s) => s === 'strict-void' || s === 'strict-at-risk').length;
  const voided = list.filter((s) => s === 'strict-void').length;
  if (atRisk <= 0) return { atRisk: 0, void: voided, rate: null };
  return { atRisk, void: voided, rate: voided / atRisk };
}

/** Argument objects that came back with no guarantee behind them. Pure. */
export function unvalidatedCalls(rows) {
  let total = 0;
  for (const row of rows ?? []) {
    if (row?.state === 'strict-void') total += readInt(row?.calls);
  }
  return total;
}

/** The repair for one classified turn. Pure. */
export function repairLines(state) {
  if (state === 'strict-void') {
    return [
      'set parallel_tool_calls false whenever strict schemas matter. It ' +
      'defaults to true, which is why this was never a decision anyone made.',
      'if you need the fan-out for latency, drop strict and validate the ' +
      'arguments yourself. Do not keep a guarantee you know is not held.',
      'key every tool handler on call_id and make it idempotent, so a ' +
      'duplicate parallel call cannot double-apply.',
    ];
  }
  if (state === 'strict-at-risk') {
    return ['this turn was fine and the configuration is not. The same request ' +
            'shape returns several calls whenever the model decides to, so set ' +
            'parallel_tool_calls false before it does.'];
  }
  if (state === 'fanout-no-strict') {
    return ['no schema guarantee was in place to lose. Validate tool arguments ' +
            'in your own handler, or declare strict and serialise the calls.'];
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
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const ids = parseIds(await readFile(file, 'utf8'));
  if (ids.length === 0) {
    console.error('no usable response ids. /v1/responses cannot be listed, so ' +
                  'the sample has to come from your own request log');
    process.exitCode = 2;
    return;
  }

  const rows = [];
  let bad = 0;
  let read = 0;
  for (const id of ids) {
    const body = await get(key, `/responses/${id}`);
    if (body === null) continue;
    read += 1;
    const [state, detail] = classify(body);
    const calls = functionCalls(body);
    rows.push({ id, state, calls: calls.length });

    const line = `${state.padEnd(19)} ${id.padEnd(14)} ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      console.warn(`  calls: ${calls.map((c) => c.name).join(', ')}`);
    } else if (state === 'fanout-no-strict') {
      console.warn(line);
    } else if (showAll || state === 'strict-at-risk') {
      console.log(line);
    }

    const dupes = duplicateNames(calls);
    if (Object.keys(dupes).length > 0) {
      console.warn(`  duplicate: ${Object.entries(dupes).sort()
        .map(([n, c]) => `${n} called ${c} time(s) in one turn`).join('; ')}. ` +
        'Handlers keyed on the tool name rather than call_id will double apply.');
    }

    if (state === 'strict-void' || state === 'fanout-no-strict') {
      for (const repair of repairLines(state)) console.warn(`  repair: ${repair}`);
    }
  }

  const shape = exposure(rows.map((r) => r.state));
  if (shape.rate === null) {
    console.log('no turn in this sample declared a strict tool with parallel ' +
                'calls left on, so there is no exposure to report');
  } else {
    console.log(`exposure: ${shape.void} of ${shape.atRisk} at-risk turn(s) ` +
                `fanned out (${(shape.rate * 100).toFixed(1)}%), covering ` +
                `${unvalidatedCalls(rows)} argument object(s) with no guarantee`);
    if (shape.void === 0) {
      console.warn('  every at-risk turn happened to return one call. That is ' +
                   'luck, not configuration: set parallel_tool_calls false ' +
                   'before it stops being lucky.');
    }
  }

  console.log(`${read} response(s) read, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
