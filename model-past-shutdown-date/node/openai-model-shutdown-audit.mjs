/**
 * Report OpenAI model ids whose published shutdown date has already passed.
 *
 * Read only. One GET request, no writes: give this a project key set to Read
 * Only. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// Matched longest prefix first, and deliberately family-level: this says where a
// line went, not that any one snapshot is a drop-in replacement for another.
const SUCCESSORS = [
  ['gpt-image-1', 'gpt-image-2'],
  ['chatgpt-image', 'gpt-image-2'],
  ['dall-e', 'gpt-image-2'],
  ['gpt-5-nano', 'gpt-5.6-luna'],
  ['gpt-5-mini', 'gpt-5.6-terra'],
  ['gpt-5-pro', 'gpt-5.6-sol'],
  ['gpt-5', 'gpt-5.6-sol'],
  ['o4-mini', 'gpt-5.6-terra'],
  ['o3-pro', 'gpt-5.6-sol'],
  ['o3', 'gpt-5.6-sol'],
  ['o1', 'gpt-5.6-sol'],
  ['gpt-4', 'gpt-5.6-sol'],
];

const FAILING = ['retired', 'retiring-today'];

/** The family a retired id was folded into, or null if this script has no opinion. */
export function successor(modelId) {
  for (const [prefix, replacement] of SUCCESSORS) {
    if (modelId.startsWith(prefix)) return replacement;
  }
  return null;
}

/**
 * Read a shutdown_date into a UTC date, or null when it cannot be read. The
 * field is a plain YYYY-MM-DD string; a full timestamp is tolerated by taking
 * the date part. Anything else returns null rather than a guess, because a
 * guess here either invents an outage or hides one.
 */
export function parseDay(value) {
  const raw = String(value ?? '').trim().split('T')[0];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null;
  const ms = Date.parse(`${raw}T00:00:00Z`);
  return Number.isNaN(ms) ? null : new Date(ms);
}

const DAY = 86400000;

/**
 * Classify one entry from GET /v1/models against a date you pass in. Pure, so
 * the boundary cases can be tested at a fixed date instead of at whatever day
 * the suite happens to run. Returns [state, detail].
 */
export function verdict(model, today) {
  const modelId = String(model.id ?? '').trim();
  if (!modelId) return ['unreadable', 'entry has no id field'];

  const raw = model.shutdown_date;
  if (raw === null || raw === undefined || String(raw).trim() === '') {
    return ['open',
      'no shutdown date published. That is the current state of the field, ' +
      'not a guarantee: re-read it on a schedule.'];
  }

  const day = parseDay(raw);
  if (day === null) {
    return ['unreadable-date',
      `shutdown_date is ${JSON.stringify(raw)}, which is not a date this ` +
      'script will guess at. Check it by hand.'];
  }

  const iso = day.toISOString().slice(0, 10);
  const days = Math.round((day.getTime() - today.getTime()) / DAY);
  if (days < 0) {
    return ['retired',
      `shut down on ${iso}, ${-days} day(s) ago. Calls naming this id return ` +
      '404 model_not_found, which is the same error a misspelled model name returns.'];
  }
  if (days === 0) {
    return ['retiring-today',
      `shuts down today (${iso}). Requests may already be failing; treat this ` +
      'as an outage in progress, not a warning.'];
  }
  return ['scheduled',
    `shuts down on ${iso}, ${days} day(s) from now. Still routable today.`];
}

async function get(key, path) {
  const res = await fetch(API + path, {
    headers: { Authorization: `Bearer ${key}` },
  });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: the key is wrong, revoked, or belongs to ' +
                    'another organization');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }

  const wanted = new Set(process.argv.reduce((acc, arg, i) => (
    arg === '--model' && process.argv[i + 1] ? [...acc, process.argv[i + 1]] : acc
  ), []));
  const showAll = process.argv.includes('--show-all');

  const { data = [] } = await get(key, '/models');
  if (data.length === 0) {
    console.log('the models list came back empty for this key');
    return;
  }

  let models = data;
  if (wanted.size > 0) {
    const listed = new Set(data.map((m) => String(m.id ?? '')));
    for (const missing of [...wanted].filter((m) => !listed.has(m)).sort()) {
      console.warn(`${'absent'.padEnd(15)} ${missing}  not in the models list at ` +
        'all, so there is no shutdown_date left to read. An id that has been ' +
        'dropped from the list is already gone.');
    }
    models = data.filter((m) => wanted.has(String(m.id ?? '')));
  }

  const today = new Date(`${new Date().toISOString().slice(0, 10)}T00:00:00Z`);
  let bad = 0;
  for (const model of [...models].sort((a, b) =>
    String(a.id ?? '').localeCompare(String(b.id ?? '')))) {
    const [state, detail] = verdict(model, today);
    const modelId = String(model.id ?? '?');
    const line = `${state.padEnd(15)} ${modelId}  ${detail}`;
    if (FAILING.includes(state)) {
      bad += 1;
      console.warn(line);
      const replacement = successor(modelId);
      console.warn(replacement
        ? `  repair: change model="${modelId}" to model="${replacement}" at ` +
          'every call site, then read shutdown_date on the new id'
        : '  repair: take the replacement from the deprecations page and pin it');
    } else if (state === 'unreadable' || state === 'unreadable-date') {
      console.warn(line);
    } else if (showAll || state === 'scheduled') {
      console.log(line);
    }
  }

  console.log(`${models.length} model id(s) checked, ${bad} past their shutdown date`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
