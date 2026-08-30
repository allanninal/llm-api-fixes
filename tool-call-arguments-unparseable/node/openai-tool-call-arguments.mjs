/**
 * Check every stored tool call against the tool schema declared beside it.
 *
 * Read only. GET /v1/responses/{response_id} for each id you supply, with a
 * project key set to Read Only. There is no list endpoint for /v1/responses,
 * so the ids come from your own records: one id per line in a file.
 *
 * Function arguments come back JSON encoded, as a string, and the docs are
 * explicit that the string may be malformed. Two different faults arrive
 * through that one field: a string that will not parse, which a careful
 * try/catch handles, and a string that parses perfectly and describes a call
 * your handler cannot accept, which nothing around JSON.parse will catch.
 *
 * The response carries the tool definitions it was generated with, so the
 * declared schema and the emitted call can be compared without reading a line
 * of your source. The repair is printed, never performed.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

const FINDINGS = new Set([
  'arguments-violate-schema', 'arguments-unparseable', 'unknown-tool']);

/** What a parsed JSON value actually is, in schema vocabulary. Pure. */
export function typeName(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  return typeof value;
}

const TYPE_TESTS = {
  object: (v) => v !== null && typeof v === 'object' && !Array.isArray(v),
  array: (v) => Array.isArray(v),
  string: (v) => typeof v === 'string',
  boolean: (v) => typeof v === 'boolean',
  null: (v) => v === null,
  number: (v) => typeof v === 'number' && Number.isFinite(v),
  integer: (v) => typeof v === 'number' && Number.isInteger(v),
};

/** Every function call in a stored response, in emission order. Pure. */
export function functionCalls(response) {
  const calls = [];
  for (const item of response?.output ?? []) {
    if (!item || typeof item !== 'object' || item.type !== 'function_call') continue;
    calls.push({ name: String(item.name ?? ''),
      callId: String(item.call_id ?? item.id ?? ''),
      arguments: item.arguments });
  }
  for (const choice of response?.choices ?? []) {
    for (const call of choice?.message?.tool_calls ?? []) {
      const fn = call?.function ?? {};
      calls.push({ name: String(fn.name ?? ''),
        callId: String(call?.id ?? ''),
        arguments: fn.arguments });
    }
  }
  return calls;
}

/** The tool definitions the response was generated with, keyed by name. Pure. */
export function declaredTools(response) {
  const tools = new Map();
  for (const tool of response?.tools ?? []) {
    if (!tool || typeof tool !== 'object') continue;
    if (String(tool.type ?? 'function') !== 'function') continue;
    const inner = (tool.function && typeof tool.function === 'object') ? tool.function : tool;
    const name = String(inner.name ?? '');
    if (name) tools.set(name, { parameters: inner.parameters, strict: inner.strict === true });
  }
  return tools;
}

/**
 * Parse one arguments string. Pure. Returns [value, error].
 * An empty string is a legal way to call a tool that takes nothing, so it
 * parses to an empty object rather than failing.
 */
export function parseArguments(text) {
  if (text === null || text === undefined) return [null, 'the arguments field is absent'];
  if (typeof text === 'object' && !Array.isArray(text)) return [text, null];
  const body = String(text).trim();
  if (!body) return [{}, null];
  let value;
  try {
    value = JSON.parse(body);
  } catch (err) {
    return [null, err.message];
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return [null, `arguments parsed to ${typeName(value)}, not an object`];
  }
  return [value, null];
}

/**
 * Where a parsed argument object departs from its declared schema. Pure.
 * Types, required keys, unexpected keys and enums: the four failures that
 * actually reach a dispatcher.
 */
export function schemaViolations(value, schema, path = 'arguments') {
  const problems = [];
  if (!schema || typeof schema !== 'object' || Object.keys(schema).length === 0) {
    return problems;
  }

  const raw = schema.type;
  const kinds = (Array.isArray(raw) ? raw : (raw ? [raw] : [])).map(String);
  const known = kinds.filter((k) => k in TYPE_TESTS);
  if (known.length && !known.some((k) => TYPE_TESTS[k](value))) {
    return [`${path}: expected ${known.join(' or ')}, got ${typeName(value)}`];
  }

  if (Array.isArray(schema.enum) && schema.enum.length && !schema.enum.includes(value)) {
    problems.push(`${path}: ${JSON.stringify(value)} is not one of the ` +
      `${schema.enum.length} declared value(s)`);
  }

  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const properties = (schema.properties && typeof schema.properties === 'object')
      ? schema.properties : {};
    for (const name of Array.isArray(schema.required) ? schema.required : []) {
      if (!(name in value)) problems.push(`${path}.${name}: required and missing`);
    }
    if (schema.additionalProperties === false) {
      for (const name of Object.keys(value).filter((k) => !(k in properties)).sort()) {
        problems.push(`${path}.${name}: not declared, and the schema forbids extra keys`);
      }
    }
    for (const name of Object.keys(value).filter((k) => k in properties).sort()) {
      problems.push(...schemaViolations(value[name], properties[name], `${path}.${name}`));
    }
  }

  if (Array.isArray(value) && schema.items && typeof schema.items === 'object') {
    value.forEach((entry, index) => {
      problems.push(...schemaViolations(entry, schema.items, `${path}[${index}]`));
    });
  }
  return problems;
}

/**
 * Classify one function call. Pure. Returns [state, detail].
 * Order matters: a call whose arguments were cut off belongs to the truncation
 * note, and saying so first keeps the reader from tuning a tool schema to fix
 * an output ceiling.
 */
export function classify(call, tools, truncated = false) {
  const name = String(call?.name ?? '');
  const map = tools instanceof Map ? tools : new Map(Object.entries(tools ?? {}));
  const [value, error] = parseArguments(call?.arguments);

  if (error !== null) {
    if (truncated) {
      return ['arguments-truncated',
        `the arguments string does not parse (${error}) and the response ` +
        'stopped on the output ceiling, so it was cut mid-write rather than ' +
        'written wrongly'];
    }
    return ['arguments-unparseable',
      `the arguments string does not parse (${error}) and the response ` +
      'completed, so nothing was constraining the grammar'];
  }

  if (!map.has(name)) {
    return ['unknown-tool',
      `the arguments parse cleanly and no tool named '${name}' was declared on ` +
      'this response. A dispatcher that indexes a handler map by name raises ' +
      'here, not at the parse.'];
  }

  const tool = map.get(name);
  const problems = schemaViolations(value, tool.parameters);
  if (problems.length) {
    return ['arguments-violate-schema',
      `the arguments parse cleanly and break the declared schema in ` +
      `${problems.length} place(s): ${problems.join('; ')}`];
  }

  if (!tool.strict) {
    return ['dispatchable-unconstrained',
      'this call matches the schema, but the tool was declared without ' +
      'strict: true, so nothing guaranteed that it would'];
  }
  return ['dispatchable', 'parses and matches the declared schema'];
}

/** The repair for one state. Pure. */
export function repairLines(state, name) {
  if (state === 'arguments-violate-schema') {
    return ['Validate arguments against the tool schema before dispatch, and feed ' +
      'the validation error back to the model as the tool result so it can correct ' +
      'itself. A crashed turn teaches the model nothing; a returned error usually ' +
      'fixes the next call.',
    `Set strict: true on tool ${name ?? 'this tool'}, with additionalProperties: ` +
      'false and every parameter listed in required. Without it the schema is a ' +
      'suggestion.'];
  }
  if (state === 'arguments-unparseable') {
    return ['Wrap every argument parse in try/catch and return the parse error to ' +
      'the model as the tool result rather than raising through the turn.',
    'Set strict: true on the tool so constrained decoding holds the grammar in ' +
      'the first place.'];
  }
  if (state === 'arguments-truncated') {
    return ['Not a schema problem. The output ceiling cut the argument string ' +
      'mid-write, so raise it and check the response status before touching any ' +
      'tool call.'];
  }
  if (state === 'unknown-tool') {
    return ['Handle an unknown tool name explicitly: return a tool result saying ' +
      'the tool does not exist. A thrown lookup error out of the handler map ends ' +
      'the turn and loses the conversation state.',
    'Check that the tool list sent on this call matches the handler map. A tool ' +
      'renamed on one side only produces exactly this.'];
  }
  if (state === 'dispatchable-unconstrained') {
    return ['This call was fine. Set strict: true on the tool anyway, because ' +
      'nothing about this response promised it would be.'];
  }
  return [];
}

/** Did this response stop on the output ceiling? Pure. */
export function wasTruncated(response) {
  if (String(response?.status ?? '') === 'incomplete') {
    return String(response?.incomplete_details?.reason ?? '') === 'max_output_tokens';
  }
  for (const choice of response?.choices ?? []) {
    if (String(choice?.finish_reason ?? '') === 'length') return true;
  }
  return false;
}

async function fetchResponse(key, responseId) {
  const res = await fetch(`${API}/responses/${responseId}`,
    { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 404) return null;
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: this needs a project key that ` +
                    'can read stored responses');
  }
  if (!res.ok) throw new Error(`${res.status} from /responses/${responseId}`);
  return res.json();
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const idsFile = (process.env.RESPONSE_IDS || "dummy-response-ids");
  if (!key || !idsFile) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only) and ' +
                  'RESPONSE_IDS (a file of stored response ids, one per line)');
    process.exitCode = 2;
    return;
  }
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const ids = (await readFile(idsFile, 'utf8')).split('\n')
    .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));

  let checked = 0;
  let bad = 0;
  for (const responseId of ids) {
    const stored = await fetchResponse(key, responseId);
    if (stored === null) {
      console.warn(`${'unreadable'.padEnd(27)} ${responseId}  not found. Stored ` +
        'responses expire, and a response created without storage was never readable.');
      continue;
    }
    const tools = declaredTools(stored);
    const truncated = wasTruncated(stored);
    const calls = functionCalls(stored);
    if (!calls.length) continue;
    if (calls.length > 1) {
      console.log(`${'parallel-calls'.padEnd(27)} ${responseId}  ${calls.length} ` +
        'call(s) in one turn');
    }
    for (const call of calls) {
      checked += 1;
      const [state, detail] = classify(call, tools, truncated);
      const line = `${state.padEnd(27)} ${responseId} ${call.name}/` +
        `${call.callId || '-'}  ${detail}`;
      if (FINDINGS.has(state)) {
        bad += 1;
        console.warn(line);
        for (const repair of repairLines(state, call.name)) console.warn(`  repair: ${repair}`);
      } else if (state === 'dispatchable') {
        if (showAll) console.log(line);
      } else {
        console.log(line);
        for (const repair of repairLines(state, call.name)) console.log(`  note: ${repair}`);
      }
    }
  }

  console.log(`${checked} tool call(s) checked, ${bad} your dispatcher cannot use`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
