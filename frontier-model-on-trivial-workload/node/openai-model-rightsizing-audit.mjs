/**
 * Report OpenAI models that are larger than the work they are doing.
 *
 * Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
 * organization admin key with read scopes, because every /v1/organization
 * endpoint rejects a project key outright. The repair is printed, never
 * performed.
 */
const API = 'https://api.openai.com/v1';

// Substrings that mean "this is already the small sibling".
const SMALL_MARKERS = ['mini', 'nano', 'small', 'lite', 'embedding', 'moderation'];

// The families worth right-sizing, each mapped to the cheaper sibling that
// answers the same shape of question. A table rather than a string rule,
// because a wrong suggestion here is worse than no suggestion.
const SIBLINGS = [
  ['gpt-5', 'gpt-5-mini'],
  ['gpt-4.1', 'gpt-4.1-mini'],
  ['gpt-4o', 'gpt-4o-mini'],
  ['o3', 'o4-mini'],
  ['o1', 'o4-mini'],
];

const FINDINGS = ['oversized'];

/**
 * Classify a model id. Pure, and deliberately conservative: "unknown" is not a
 * finding, because a model this table has never heard of is one this script has
 * no business advising on.
 */
export function tier(model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return 'unknown';
  if (name.startsWith('ft:')) return 'custom';
  if (SMALL_MARKERS.some((m) => name.includes(m))) return 'small';
  for (const [family] of SIBLINGS) if (name.startsWith(family)) return 'premium';
  return 'unknown';
}

/** The cheaper model answering the same shape of question, or null. Pure. */
export function sibling(model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (tier(name) !== 'premium') return null;
  for (const [family, cheaper] of SIBLINGS) {
    if (name.startsWith(family)) return cheaper;
  }
  return null;
}

/**
 * Sum the daily buckets into one row per model. Pure.
 *
 * Folding before dividing matters: a mean taken per bucket and then averaged
 * weights a quiet Sunday as heavily as a Tuesday.
 */
export function fold(pages) {
  const out = new Map();
  for (const page of pages) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const model = String(result.model ?? '').trim();
        if (!model) continue;
        if (!out.has(model)) {
          out.set(model, { requests: 0, input: 0, output: 0, projects: new Set() });
        }
        const row = out.get(model);
        for (const [field, key] of [['num_model_requests', 'requests'],
                                    ['input_tokens', 'input'],
                                    ['output_tokens', 'output']]) {
          const n = Number(result[field] ?? 0);
          if (Number.isFinite(n)) row[key] += Math.trunc(n);
        }
        if (result.project_id) row.projects.add(String(result.project_id));
      }
    }
  }
  const folded = {};
  for (const [model, row] of out) {
    folded[model] = { ...row, projects: [...row.projects].sort() };
  }
  return folded;
}

/**
 * Classify one folded model row. Pure. Returns [state, detail].
 * Short answers over enormous prompts are separated out, because the money
 * there is on the input side and swapping the model saves almost none of it.
 */
export function verdict(model, row, minRequests = 500, trivialOutput = 50,
                        longInput = 20000) {
  const requestsMade = Number(row.requests ?? 0);
  if (!Number.isFinite(requestsMade)) {
    return ['unreadable',
      'num_model_requests did not sum to a number, so there is no denominator ' +
      'and no ratio to read'];
  }
  if (requestsMade <= 0) {
    return ['unreadable', '0 request(s) in the window, so there is nothing to divide by'];
  }
  if (requestsMade < minRequests) {
    return ['low-volume',
      `${requestsMade} request(s) in the window, under the floor of ` +
      `${minRequests}. A mean taken over this few calls is noise, not a shape.`];
  }

  const outPer = Number(row.output ?? 0) / requestsMade;
  const inPer = Number(row.input ?? 0) / requestsMade;
  const shape = `${requestsMade} request(s), mean output ${outPer.toFixed(0)} ` +
                `token(s), mean input ${inPer.toFixed(0)} token(s)`;

  const kind = tier(model);
  if (kind === 'custom') {
    return ['custom-model',
      `${shape}. This is a fine-tune, and its size is inherited from the base ` +
      'model rather than chosen here.'];
  }
  if (kind === 'small') {
    return ['right-sized', `${shape}. Already the cheap sibling for its family.`];
  }
  if (kind !== 'premium') {
    return ['unknown-model',
      `${shape}. No cheaper sibling is known for this model id, so this script ` +
      'has no recommendation to make about it.'];
  }

  if (outPer >= trivialOutput) {
    return ['deliberative',
      `${shape}. The answers are long enough that the model is plausibly doing ` +
      'the work it was chosen for.'];
  }
  if (inPer >= longInput) {
    return ['input-bound',
      `${shape}. Short answers over very large prompts. The bill here is input, ` +
      'not model tier, so caching the prefix will save more than downgrading ' +
      'the model.'];
  }
  return ['oversized',
    `${shape}. A premium model returning answers this short is answering ` +
    'questions a cheaper sibling would answer identically.'];
}

/**
 * Can this project still reach this model? Pure. An unconstrained project is
 * the durable half of the finding: without a restriction the expensive model
 * comes back the next time somebody copies a snippet from the quickstart.
 */
export function permissionsState(perms, model) {
  if (perms === null || typeof perms !== 'object' || Array.isArray(perms)) {
    return 'unreadable';
  }
  const mode = String(perms.mode ?? '').trim().toLowerCase();
  const ids = (Array.isArray(perms.model_ids) ? perms.model_ids : [])
    .map((i) => String(i).trim().toLowerCase());
  const name = String(model ?? '').trim().toLowerCase();

  if (mode === 'allow_list') {
    if (ids.length === 0) return 'blocked';
    return ids.includes(name) ? 'allowed' : 'blocked';
  }
  if (mode === 'deny_list') {
    if (ids.length === 0) return 'unconstrained';
    return ids.includes(name) ? 'blocked' : 'allowed';
  }
  return 'unreadable';
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: OPENAI_ADMIN_KEY must be an organization ' +
                    'admin key, not a project key');
  }
  if (res.status === 403) {
    throw new Error('403 from OpenAI: the key is not authorised for ' +
                    '/v1/organization. A project key cannot read usage.');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function usagePages(key, startTime, days, maxPages = 20) {
  const pages = [];
  let params = {
    start_time: startTime, bucket_width: '1d', limit: days,
    group_by: ['model', 'project_id'],
  };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/usage/completions', params);
    pages.push(page);
    if (!page.next_page) break;
    params = { ...params, page: page.next_page };
  }
  return pages;
}

async function spendByLineItem(key, startTime) {
  const out = {};
  const page = await get(key, '/organization/costs',
    { start_time: startTime, limit: 31, group_by: 'line_item' });
  for (const bucket of page.data ?? []) {
    for (const result of bucket.results ?? []) {
      const item = String(result.line_item ?? '');
      const amount = Number(result.amount?.value ?? 0);
      if (Number.isFinite(amount)) out[item] = (out[item] ?? 0) + amount;
    }
  }
  return out;
}

/**
 * Spend on exactly this model, from the cost report's line items. Pure.
 * Substring matching is not good enough: "gpt-5" occurs inside "gpt-5-mini,
 * input tokens" and inside a fine-tune id built on it, and quoting either as
 * the premium model's spend overstates the saving in the one line a reader is
 * going to act on. Model ids only contain letters, digits, dots and dashes, so
 * the escape is a pair of character classes rather than a backslash dance.
 */
export function spendFor(model, spend) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return 0;
  const escaped = name.replace(/[.-]/g, (c) => `[${c}]`);
  const pattern = new RegExp(`(?<![-a-z0-9.:])${escaped}(?![-a-z0-9.])`);
  let total = 0;
  for (const [item, amount] of Object.entries(spend ?? {})) {
    if (pattern.test(String(item).toLowerCase())) {
      const value = Number(amount);
      if (Number.isFinite(value)) total += value;
    }
  }
  return total;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read scopes)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 14);
  const minRequests = Number((process.env.MIN_REQUESTS || "dummy-min-requests") ?? 500);
  const trivialOutput = Number((process.env.TRIVIAL_OUTPUT || "dummy-trivial-output") ?? 50);
  const showAll = process.argv.includes('--show-all');

  const now = Math.floor(Date.now() / 1000);
  const rows = fold(await usagePages(key, now - days * 86400, days));
  const spend = await spendByLineItem(key, now - 30 * 86400);

  let checked = 0;
  let bad = 0;
  for (const model of Object.keys(rows).sort()) {
    const row = rows[model];
    const [state, detail] = verdict(model, row, minRequests, trivialOutput);
    checked += 1;
    const line = `${state.padEnd(14)} ${model.padEnd(16)} ${detail}`;

    if (FINDINGS.includes(state)) {
      bad += 1;
      console.warn(line);
      const cheaper = sibling(model);
      console.warn(`  repair: ${cheaper} answers this shape of question; 30d ` +
                   `spend on ${model} was $${spendFor(model, spend).toFixed(2)}`);
      for (const project of row.projects) {
        const perms = await get(key,
          `/organization/projects/${project}/model_permissions`);
        const where = permissionsState(perms, model);
        if (where === 'unconstrained') {
          console.warn(`  repair: project ${project} is unconstrained. To make ` +
            `the change durable, set model_permissions to mode allow_list with ` +
            `model_ids ['${cheaper}'] so the expensive model cannot come back.`);
        } else {
          console.warn(`  note: project ${project} model_permissions say ${where}`);
        }
      }
    } else if (state === 'input-bound') {
      console.warn(line);
      console.warn('  repair: read the prompt-caching note before changing the ' +
                   'model. A stable prefix at this size is the bill.');
    } else if (state === 'unreadable') {
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${checked} model(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
