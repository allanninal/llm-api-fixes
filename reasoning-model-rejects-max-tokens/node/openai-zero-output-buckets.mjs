/**
 * Find OpenAI usage buckets that counted requests and generated nothing.
 *
 * Read only. One GET against the organization usage report, which needs an
 * organization admin key (sk-admin-), plus an optional GET /v1/models/{id}
 * with a project key set to Read Only.
 *
 * num_model_requests above zero with output_tokens at zero is a set of calls
 * that never reached generation; no input tokens with it means the body was
 * rejected before the prompt was read. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// Whole-id prefixes. A substring test for "o" would match gpt-4o.
const REASONING_PREFIXES = ['o1', 'o3', 'o4', 'gpt-5'];

const FINDINGS = new Set(['parameter-rejected', 'partial-rejection']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Is this id one of the families that refuse max_tokens? Pure.
 * gpt-4o must be false here, or the script prints a rename that does not apply.
 */
export function isReasoningModel(model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return false;
  return REASONING_PREFIXES.some(
    (p) => name === p || name.startsWith(`${p}-`) || name.startsWith(`${p}.`));
}

/**
 * Fold usage buckets into one row per (project, model). Pure.
 * Silent buckets are counted rather than summed away: all of them and one in
 * twelve are a broken deploy and a half-finished rollout.
 */
export function fold(buckets) {
  const rows = new Map();
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const key = `${result?.project_id ?? 'unknown'}\u0000${result?.model ?? 'unknown'}`;
      if (!rows.has(key)) {
        rows.set(key, { project: String(result?.project_id ?? 'unknown'),
                        model: String(result?.model ?? 'unknown'),
                        requests: 0, input: 0, output: 0, buckets: 0,
                        silentBuckets: 0, silentRequests: 0, silentInput: 0 });
      }
      const row = rows.get(key);
      const made = readInt(result?.num_model_requests);
      const read = readInt(result?.input_tokens);
      const wrote = readInt(result?.output_tokens);
      row.requests += made;
      row.input += read;
      row.output += wrote;
      row.buckets += 1;
      if (made > 0 && wrote === 0) {
        row.silentBuckets += 1;
        row.silentRequests += made;
        row.silentInput += read;
      }
    }
  }
  return rows;
}

/** Share of a row's requests that generated no output. Pure. Null when none. */
export function silentShare(row) {
  const made = readInt(row?.requests);
  if (made <= 0) return null;
  return Math.min(1, readInt(row?.silentRequests) / made);
}

/**
 * Classify one (project, model) row. Pure. Returns [state, detail].
 * The split is on input tokens inside the silent buckets: none means the body
 * was rejected on validation, some means generation was blocked instead.
 */
export function classify(model, row, minRequests = 50, partialFloor = 0.2,
                         totalFloor = 0.99) {
  const made = readInt(row?.requests);
  if (made < minRequests) {
    return ['too-few-requests',
      `${made} request(s) in the window, under the floor of ${minRequests}. ` +
      'A silence this small is not evidence of anything.'];
  }

  const share = silentShare(row) ?? 0;
  const shape = `${made} request(s) over ${readInt(row?.buckets)} bucket(s), ` +
    `${readInt(row?.input)} input token(s) and ${readInt(row?.output)} output token(s)`;

  if (share >= totalFloor) {
    if (readInt(row?.silentInput) === 0) {
      return ['parameter-rejected',
        `${shape}. Nothing was read and nothing was generated, so these calls ` +
        'were rejected on the request body before the prompt was processed.'];
    }
    return ['generation-blocked',
      `${shape}. The prompt was read and nothing came back, which is not a ` +
      'refused parameter name: look at organization verification, a content ' +
      'filter, or an output cap of zero.'];
  }

  if (share >= partialFloor) {
    return ['partial-rejection',
      `${shape}, and ${(share * 100).toFixed(0)}% of those requests generated ` +
      'nothing. Part of the fleet is still sending the old field.'];
  }

  return ['generating', `${shape}.`];
}

/** The exact request-body repair for one model id. Pure. */
export function repairLines(model) {
  if (isReasoningModel(model)) {
    return [
      'Chat Completions: send max_completion_tokens instead of max_tokens, and ' +
      'raise the number. The cap now has to absorb reasoning tokens as well as ' +
      'the visible answer.',
      'Responses API: the same field is called max_output_tokens.',
      'Remove temperature, top_p, presence_penalty, frequency_penalty and ' +
      'logprobs for this model and express the intent as a reasoning effort ' +
      'setting. Do not send temperature 1 explicitly; omit it.',
    ];
  }
  return [
    'This id is not one of the reasoning families, so a refused parameter name ' +
    'is the less likely cause here. Read one 400 body for its code and param ' +
    'fields before changing anything.',
  ];
}

/** What the model lookup says about whose fault the failure is. Pure. */
export function modelVerdict(status) {
  if (status === null || status === undefined) {
    return ['unchecked',
      'no project key was supplied, so the model id itself was not checked'];
  }
  if (status === 200) {
    return ['id-resolves',
      'the id resolves for this key, so the fault is in the request body and ' +
      'not in access'];
  }
  if (status === 404) {
    return ['id-unreachable',
      'the id does not resolve for this key. That is retirement or entitlement ' +
      'rather than a parameter name, and it is a different repair'];
  }
  if (status === 401 || status === 403) {
    return ['check-refused',
      'the project key could not read the model list, so the id was not ' +
      'confirmed either way'];
  }
  return ['check-inconclusive', `the model lookup returned ${status}`];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* pages(key, path, params, maxPages = 40) {
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, path, query);
    for (const bucket of page?.data ?? []) yield bucket;
    if (!page?.has_more || !page?.next_page) return;
    query = { ...params, page: page.next_page };
  }
}

async function checkModel(key, model) {
  if (!key) return null;
  try {
    const res = await fetch(`${API}/models/${model}`,
                            { headers: { Authorization: `Bearer ${key}` } });
    return res.status;
  } catch {
    return null;
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key; read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }
  const projectKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const hours = Math.max(1, Math.min(Number((process.env.HOURS || "dummy-hours") ?? 24), 168));
  const minRequests = Number((process.env.MIN_REQUESTS || "dummy-min-requests") ?? 50);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of pages(admin, '/organization/usage/completions', {
    start_time: Math.floor(Date.now() / 1000) - hours * 3600,
    bucket_width: '1h',
    limit: hours,
    group_by: ['model', 'project_id'],
  })) buckets.push(bucket);

  const rows = fold(buckets);
  if (rows.size === 0) {
    console.log(`no completions usage in the last ${hours} hour(s)`);
    return;
  }

  let checked = 0;
  let bad = 0;
  const ordered = [...rows.values()].sort((a, b) => b.requests - a.requests);
  for (const row of ordered) {
    const [state, detail] = classify(row.model, row, minRequests);
    checked += 1;
    const line = `${state.padEnd(19)} ${row.project} / ${row.model}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      const [, note] = modelVerdict(await checkModel(projectKey, row.model));
      console.warn(`  ${note}`);
      for (const repair of repairLines(row.model)) console.warn(`  repair: ${repair}`);
    } else if (state === 'generation-blocked') {
      console.warn(line);
      console.warn('  repair: this is not the parameter rename. Check organization ' +
                   'verification for the streaming path and the project model ' +
                   'permissions before touching the request body.');
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${checked} model/project row(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
