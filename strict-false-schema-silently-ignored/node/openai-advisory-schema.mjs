/**
 * Find stored OpenAI responses whose JSON schema was never actually enforced.
 *
 * Read only. GET /v1/responses/{response_id} for each id you supply, with a
 * project key set to Read Only. There is no list endpoint for /v1/responses,
 * so the ids come from your own records: one id per line in a file.
 *
 * Structured Outputs guarantees schema adherence only when strict is true.
 * With strict absent or false the schema degrades to a hint the model usually
 * follows, and the request is accepted either way with no warning. The stored
 * response echoes the format it was given, which is the only place outside
 * your source tree where the flag can be read back.
 *
 * When strict is off, the interesting question is why, so this script walks
 * the schema and prints every rule that would have to be fixed first.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

const FINDINGS = new Set(['advisory-schema', 'no-schema', 'advisory-tools']);

// Constrained decoding ignores these entirely.
const UNENFORCED_KEYWORDS = ['minLength', 'maxLength', 'pattern', 'format',
  'minimum', 'maximum', 'multipleOf', 'minItems', 'maxItems', 'uniqueItems',
  'default'];

const MAX_DEPTH = 5;
const MAX_PROPERTIES = 5000;
const MAX_ENUM_VALUES = 1000;

/**
 * The output format the response was generated under. Pure.
 * Returns [kind, name, strict, schema]. Read from the response, not from your
 * source tree: the constant in the repository is not necessarily what the
 * running deploy sent.
 */
export function declaredFormat(response) {
  const fmt = response?.text?.format ?? response?.response_format ?? {};
  if (!fmt || typeof fmt !== 'object' || Object.keys(fmt).length === 0) {
    return ['none', null, null, null];
  }
  const kind = String(fmt.type ?? 'none');
  if (kind === 'json_schema') {
    const inner = (fmt.json_schema && typeof fmt.json_schema === 'object')
      ? fmt.json_schema : fmt;
    return ['json_schema', inner.name ?? null, inner.strict ?? null,
      inner.schema ?? null];
  }
  return [kind, null, null, null];
}

/** What the declared format actually promises. Pure. */
export function strictState(kind, strict) {
  if (kind === 'json_schema') return strict === true ? 'enforced' : 'advisory';
  if (kind === 'json_object') return 'no-schema';
  if (kind === 'text' || kind === 'none') return 'free-text';
  return 'unknown-format';
}

/** Count properties, depth and the largest enum in a schema. Pure. */
export function schemaSize(schema, depth = 1) {
  const totals = { properties: 0, depth, enum: 0 };
  if (!schema || typeof schema !== 'object') return totals;
  if (Array.isArray(schema.enum)) totals.enum = Math.max(totals.enum, schema.enum.length);

  const children = [];
  if (schema.properties && typeof schema.properties === 'object') {
    const values = Object.values(schema.properties);
    totals.properties += values.length;
    children.push(...values);
  }
  if (schema.items && typeof schema.items === 'object') children.push(schema.items);
  for (const group of ['anyOf', 'oneOf', 'allOf']) {
    if (Array.isArray(schema[group])) {
      children.push(...schema[group].filter((x) => x && typeof x === 'object'));
    }
  }
  if (schema.$defs && typeof schema.$defs === 'object') {
    children.push(...Object.values(schema.$defs).filter((x) => x && typeof x === 'object'));
  }

  for (const child of children) {
    const below = schemaSize(child, depth + 1);
    totals.properties += below.properties;
    totals.depth = Math.max(totals.depth, below.depth);
    totals.enum = Math.max(totals.enum, below.enum);
  }
  return totals;
}

/**
 * Every reason strict: true would be refused for this schema. Pure.
 * Telling somebody to set strict: true is useless on its own: they tried, the
 * request 400ed, and the flag came back out. This list is the actual work.
 */
export function schemaBlockers(schema, path = '$', depth = 1) {
  const problems = [];
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
    return depth === 1 ? [`${path}: not a schema object`] : problems;
  }

  const raw = schema.type;
  const kinds = (Array.isArray(raw) ? raw : (raw ? [raw] : [])).map(String);

  if (depth === 1) {
    if (['anyOf', 'oneOf', 'allOf'].some((g) => schema[g])) {
      problems.push('$: the root may not be anyOf, oneOf or allOf; it must be ' +
        'a plain object');
    } else if (!kinds.includes('object')) {
      problems.push(`$: the root type must be object, not ${kinds.join(', ') || 'unset'}`);
    }
  }

  if (kinds.includes('object')) {
    if (schema.additionalProperties !== false) {
      problems.push(`${path}: needs additionalProperties: false`);
    }
    const properties = (schema.properties && typeof schema.properties === 'object')
      ? schema.properties : {};
    const required = new Set(Array.isArray(schema.required) ? schema.required : []);
    const missing = Object.keys(properties).filter((k) => !required.has(k)).sort();
    if (missing.length) {
      problems.push(`${path}: every property must be listed in required; ` +
        `missing ${missing.join(', ')}. Use a nullable type for the optional ` +
        'ones rather than leaving them out.');
    }
  }

  const present = UNENFORCED_KEYWORDS.filter((k) => k in schema);
  if (present.length) {
    problems.push(`${path}: ${present.join(', ')} are silently unenforced under ` +
      'constrained decoding. Keep them for your own validator if you like, but ' +
      'do not rely on the model honouring them.');
  }

  if (depth > MAX_DEPTH) {
    problems.push(`${path}: nested ${depth} levels deep, past the limit of ${MAX_DEPTH}`);
    return problems;
  }

  if (schema.properties && typeof schema.properties === 'object') {
    for (const name of Object.keys(schema.properties).sort()) {
      problems.push(...schemaBlockers(schema.properties[name], `${path}.${name}`, depth + 1));
    }
  }
  if (schema.items && typeof schema.items === 'object') {
    problems.push(...schemaBlockers(schema.items, `${path}[]`, depth + 1));
  }
  return problems;
}

/** Tools echoed on the response whose strict flag is not true. Pure. Per tool. */
export function looseTools(response) {
  const loose = [];
  for (const tool of response?.tools ?? []) {
    if (!tool || typeof tool !== 'object') continue;
    if (String(tool.type ?? 'function') !== 'function') continue;
    const inner = (tool.function && typeof tool.function === 'object') ? tool.function : tool;
    if (inner.strict !== true) loose.push(String(inner.name ?? 'unnamed'));
  }
  return loose;
}

/**
 * Classify one stored response. Pure.
 * Nothing here reads the output text: whether this call happened to produce a
 * well-shaped object is not the point. No call under this format was obliged to.
 */
export function classify(response) {
  const [kind, name, strict] = declaredFormat(response);
  const state = strictState(kind, strict);
  const loose = looseTools(response);
  const label = name ? `schema '${name}'` : 'the declared schema';

  if (state === 'advisory') {
    return ['advisory-schema',
      `${label} was attached with strict ${strict === false ? 'false' : 'absent'}, ` +
      'so it is a hint the model usually follows rather than a guarantee. Valid ' +
      'JSON of the wrong shape is a legal outcome here.'];
  }
  if (state === 'no-schema') {
    return ['no-schema',
      'Legacy json_object mode: the output is guaranteed to be valid JSON and ' +
      'nothing else. No schema was ever attached, so no shape was ever promised.'];
  }
  if (state === 'free-text') {
    return ['free-text',
      'No output format was declared, so there is no contract to enforce and ' +
      'nothing to report.'];
  }
  if (state === 'unknown-format') {
    return ['unknown-format',
      `Format type '${kind}' is not one this script knows. Read the raw record ` +
      'before drawing a conclusion.'];
  }

  if (loose.length) {
    return ['advisory-tools',
      `The text format is strict, but ${loose.length} tool definition(s) are ` +
      `not: ${loose.join(', ')}. Tool arguments are constrained per tool, and ` +
      'an unstrict tool is unconstrained.'];
  }
  return ['enforced',
    `${label} was attached with strict: true, and no tool beside it is loose.`];
}

/** The repair, built from the schema this response actually carried. Pure. */
export function repairLines(response, state) {
  const schema = declaredFormat(response)[3];
  if (state === 'free-text' || state === 'unknown-format' || state === 'enforced') {
    return [];
  }

  const lines = [];
  if (state === 'no-schema') {
    lines.push('Move from json_object to a json_schema format with strict: true. ' +
      'JSON mode promises syntax and nothing about shape, which is why your ' +
      'validator is the first thing that ever sees the mismatch.');
  }
  if (state === 'advisory-tools') {
    lines.push('Set strict: true on every tool as well as on the text format, ' +
      'with additionalProperties: false and every parameter listed in required.');
  }

  const blockers = schema ? schemaBlockers(schema) : [];
  if (blockers.length) {
    lines.push('strict: true would be refused for this schema until these are fixed:');
    lines.push(...blockers.map((b) => `  ${b}`));
  } else if (state === 'advisory-schema') {
    lines.push('This schema already satisfies the strict subset, so setting ' +
      'strict: true is a one-line change. Somebody dropped the flag and the ' +
      'request kept succeeding.');
  }

  if (schema) {
    const size = schemaSize(schema);
    if (size.properties > MAX_PROPERTIES) {
      lines.push(`The schema declares ${size.properties} properties, past the ` +
        `limit of ${MAX_PROPERTIES}.`);
    }
    if (size.enum > MAX_ENUM_VALUES) {
      lines.push(`The largest enum holds ${size.enum} values, past the limit of ` +
        `${MAX_ENUM_VALUES}.`);
    }
  }
  return lines;
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
    checked += 1;
    if (stored === null) {
      console.warn(`${'unreadable'.padEnd(18)} ${responseId}  not found. Stored ` +
        'responses expire, and a response created without storage was never readable.');
      continue;
    }
    const [state, detail] = classify(stored);
    const line = `${state.padEnd(18)} ${responseId}  ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      for (const repair of repairLines(stored, state)) console.warn(`  repair: ${repair}`);
    } else if (showAll || (state !== 'enforced' && state !== 'free-text')) {
      console.log(line);
    }
  }

  console.log(`${checked} response(s) checked, ${bad} with a schema nobody was ` +
              'holding to');
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
