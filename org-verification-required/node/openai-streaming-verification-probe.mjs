/**
 * Find a model that one key can list and another key cannot generate with.
 *
 * Read only. Two GET endpoints: the organization usage report with an admin
 * read key, and /v1/models/{id} with a Read Only project key. No request body
 * is ever constructed and nothing here sends a completion.
 *
 * The subject is a contrast, not a row. A fault that lives in the model
 * refuses every key; a gate on one route does not. What cannot be read is
 * stated rather than guessed: no endpoint reports verification state.
 */
const API = 'https://api.openai.com/v1';

const FINDINGS = new Set(['verification-suspected']);

export const MEASURED =
  'requests on one key were rejected before generation, on a model another key '
  + 'is generating with normally';
export const INFERRED =
  'organization verification, which gates streaming and reasoning summaries. No '
  + 'endpoint reports verification state, so this is the most likely cause and '
  + 'not a reading';

const int = (v) => {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
};

/** [[model, apiKeyId, requests, input, output]]. Pure. */
export function flatten(buckets) {
  const rows = [];
  for (const bucket of buckets ?? []) {
    for (const entry of (bucket ?? {}).results ?? []) {
      const row = entry ?? {};
      rows.push([String(row.model ?? '(unattributed)'),
                 String(row.api_key_id ?? '(unattributed)'),
                 int(row.num_model_requests), int(row.input_tokens),
                 int(row.output_tokens)]);
    }
  }
  return rows;
}

/** {model: {apiKeyId: {requests, input, output}}}. Pure. Summed. */
export function byModel(rows) {
  const out = {};
  for (const [model, keyId, requests, input, output] of rows ?? []) {
    const perModel = (out[model] ??= {});
    const slot = (perModel[keyId] ??= { requests: 0, input: 0, output: 0 });
    slot.requests += requests;
    slot.input += input;
    slot.output += output;
  }
  return out;
}

/** What one key did on one model. Pure. One of four words. */
export function keyState(row, minRequests = 1) {
  const r = row ?? {};
  if (int(r.requests) < Math.max(1, int(minRequests))) return 'idle';
  if (int(r.output) > 0) return 'producing';
  if (int(r.input) > 0) return 'no-output';
  return 'mute';
}

/** The note itself. Pure. Returns [state, detail]. */
export function contrast(perKey, minRequests = 1) {
  const rows = { ...(perKey ?? {}) };
  const states = Object.fromEntries(
    Object.entries(rows).map(([k, v]) => [k, keyState(v, minRequests)]));
  const pick = (want) => Object.keys(states).filter((k) => states[k] === want).sort();
  const mute = pick('mute');
  const producing = pick('producing');
  const silent = pick('no-output');
  const active = [...mute, ...producing, ...silent];

  if (!active.length) return ['no-traffic', 'no key sent enough requests to grade'];
  if (mute.length && producing.length) {
    const n = (v) => v.toLocaleString('en-US');
    return ['verification-suspected',
      `${mute[0]} billed ${n(rows[mute[0]].requests)} request(s) with no tokens `
      + `either side while ${producing[0]} produced ${n(rows[producing[0]].output)} `
      + 'output token(s) on the same model in the same window'];
  }
  if (mute.length && active.length === 1) {
    return ['single-key-model',
      `${mute[0]} is the only key with traffic on this model, so there is `
      + 'nothing to compare it against'];
  }
  if (mute.length) {
    return ['model-wide-mute',
      `all ${active.length} key(s) with traffic are mute, so this is a property `
      + 'of the model or the body every caller sends'];
  }
  if (silent.length && !producing.length) {
    return ['input-without-output',
      `${silent.length} key(s) consumed input and produced no output, which is a `
      + 'request that ran rather than one that was refused'];
  }
  return ['healthy', `${producing.length} key(s) with traffic, all producing output`];
}

/** Combine reachability with the contrast. Pure. Returns [state, detail]. */
export function verdict(modelStatus, perKey, minRequests = 1) {
  const [state, detail] = contrast(perKey, minRequests);
  if (modelStatus === null || modelStatus === undefined) {
    return [state, detail + ' (the model id itself was not checked, so supply a '
      + 'project key to rule out access)'];
  }
  const status = int(modelStatus);
  if (status === 404) {
    return ['model-not-visible',
      'the id does not resolve for the project key. That is retirement or '
      + 'entitlement rather than a gated feature, and it belongs to the '
      + 'model-list note'];
  }
  if (status === 401 || status === 403) {
    return [state, detail + ' (the model lookup was refused, so access was not '
      + 'confirmed either way)'];
  }
  if (status !== 200) return [state, detail + ` (the model lookup returned ${status})`];
  return [state, detail];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'verification-suspected') {
    return [
      'measured: ' + MEASURED,
      'inferred: ' + INFERRED,
      'verify the organization in Console, then allow up to 15 minutes to '
      + 'propagate. One government ID verifies one organization per 90 days, '
      + 'which matters if several organizations share an owner.',
      'as a stopgap on the affected route only, unset stream and buffer the '
      + 'whole response, and remove reasoning summary requests. Leave the batch '
      + 'and evaluation routes alone; they are already working.',
      'if the organization is already verified, the next candidate is a '
      + 'parameter that route sends and the working key does not. Diff the two '
      + 'request builders before changing anything in Console.',
    ];
  }
  if (state === 'model-wide-mute') {
    return ['not this note. Read the reasoning-model parameter note: max_tokens, '
      + 'temperature and top_p are refused by name on those families, and a '
      + 'refusal by name hits every key.'];
  }
  if (state === 'single-key-model') {
    return ['route a canary through a second key on the same model, or read the '
      + 'verification setting in Console. With one key there is no contrast, and '
      + 'this script will not invent one.',
      'measured: requests were rejected before generation on the only key that '
      + 'uses this model. Nothing more than that.'];
  }
  if (state === 'model-not-visible') {
    return ['check the id against GET /v1/models first. A model that does not '
      + 'resolve is a retirement or entitlement question, and it has a different '
      + 'repair from a gated capability.'];
  }
  if (state === 'input-without-output') {
    return ['these requests reached the model and returned nothing, which is '
      + 'truncation or a refusal rather than a rejected body. Read the '
      + 'structured-output and refusal notes instead.'];
  }
  return [];
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
    else url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/organization/* needs an `
                    + 'organization admin key, not a project key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* pages(key, path, params, maxPages = 40) {
  let q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q = { ...q, page: page.next_page };
  }
}

async function checkModel(key, model) {
  if (!key) return null;
  try {
    const r = await fetch(`${API}/models/${model}`,
                          { headers: { Authorization: `Bearer ${key}` } });
    return r.status;
  } catch {
    return null;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key; read-only '
                  + 'scopes are enough)');
    process.exitCode = 2;
    return;
  }
  const projectKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const hours = Math.max(1, Math.min(int((process.env.USAGE_HOURS || "dummy-usage-hours") ?? 24), 168));
  const minRequests = Math.max(1, int((process.env.MIN_REQUESTS || "dummy-min-requests") ?? 20));

  const buckets = [];
  for await (const bucket of pages(admin, '/organization/usage/completions', {
    start_time: Math.floor(Date.now() / 1000) - hours * 3600,
    bucket_width: '1h',
    limit: hours,
    group_by: ['model', 'api_key_id'],
  })) buckets.push(bucket);

  const grouped = byModel(flatten(buckets));
  const models = Object.keys(grouped);
  if (!models.length) {
    console.log(`no completions usage in the last ${hours} hour(s)`);
    return;
  }
  console.log(`${models.length} model(s) with traffic in the last ${hours}h`);

  const total = (m) => Object.values(grouped[m]).reduce((a, r) => a + r.requests, 0);
  let findings = 0;

  for (const model of models.sort((a, b) => total(b) - total(a))) {
    const perKey = grouped[model];
    const [preliminary] = contrast(perKey, minRequests);
    const status = ['verification-suspected', 'single-key-model', 'model-wide-mute']
      .includes(preliminary) ? await checkModel(projectKey, model) : null;
    const [state, detail] = verdict(status, perKey, minRequests);

    console.log(`${state.padEnd(23)} ${model}: ${detail}`);
    if (status !== null && status !== undefined) console.log(`  model lookup: ${status}`);
    for (const line of repairLines(state)) {
      const prefix = (line.startsWith('measured:') || line.startsWith('inferred:'))
        ? '  ' : '  repair: ';
      console.log(prefix + line);
    }
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
