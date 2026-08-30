/**
 * Report Claude model strings that are aliases rather than pinned snapshots.
 *
 * Read only. One GET per model string and nothing else: give this a workspace
 * API key. The repair is printed, never performed.
 */
import { readFileSync } from 'node:fs';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';
const DAY = 86400000;

// A trailing -YYYYMMDD. Used only to describe an id, never to decide whether it
// is pinned: that answer comes from the API, because from the 4.6 generation on
// a dateless id is itself a snapshot and pattern-matching gets it backwards.
const DATED = /-\d{8}$/;

const BAD = ['alias', 'not-found', 'unreadable'];

/**
 * Read created_at into a UTC date, or null. The field is RFC 3339, and only the
 * date part is used.
 */
export function parseCreated(value) {
  const raw = String(value ?? '').trim().split('T')[0];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null;
  const ms = Date.parse(`${raw}T00:00:00Z`);
  return Number.isNaN(ms) ? null : new Date(ms);
}

/**
 * Compare a model string with what GET /v1/models/{id} resolved it to. `model`
 * is the returned object, or null for a 404. Pure, and `today` is passed in so
 * the age of the resolved snapshot is testable at a fixed date. Returns
 * [state, detail].
 */
export function verdict(requested, model, today) {
  const asked = String(requested ?? '').trim();
  if (!asked) return ['unreadable', 'empty model string'];

  if (model === null || model === undefined) {
    return ['not-found',
      '404 not_found_error: nothing resolves this id. If a date suffix was ' +
      'appended to a 4.6-or-later id, remove it: those ids are already ' +
      'snapshots and the dated form never existed.'];
  }

  const resolved = String(model.id ?? '').trim();
  if (!resolved) return ['unreadable', 'the model object came back with no id'];

  const created = parseCreated(model.created_at);
  const age = created === null ? ''
    : ` The snapshot behind it was created ${created.toISOString().slice(0, 10)}, ` +
      `${Math.round((today.getTime() - created.getTime()) / DAY)} day(s) ago.`;

  if (resolved !== asked) {
    return ['alias',
      `an alias: it resolves to ${resolved} today, and the pointer moves ` +
      `without a deploy or an error.${age} Pin ${resolved}.`];
  }

  if (DATED.test(asked)) {
    return ['pinned', `a dated snapshot; it resolves to itself.${age}`];
  }

  return ['pinned-dateless',
    'already a pinned snapshot even though it carries no date: from the 4.6 ' +
    'generation on, the dateless id is the snapshot. Do not append a date to ' +
    `it, that id does not exist.${age}`];
}

/** The model object for one id, or null when the API returns 404. */
export async function getModel(key, modelId) {
  const res = await fetch(`${API}/models/${modelId}`, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 404) return null;
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: check ANTHROPIC_API_KEY; an ` +
                    'Admin key cannot read the models list');
  }
  if (!res.ok) throw new Error(`${res.status} from /models/${modelId}`);
  return res.json();
}

function readIds(argv) {
  const ids = [];
  argv.forEach((arg, i) => {
    if (arg === '--model' && argv[i + 1]) ids.push(argv[i + 1]);
    if (arg === '--from-file' && argv[i + 1]) {
      for (const line of readFileSync(argv[i + 1], 'utf8').split('\n')) {
        const trimmed = line.split('#')[0].trim();
        if (trimmed) ids.push(trimmed);
      }
    }
  });
  return [...new Set(ids)];
}

async function main() {
  const wanted = readIds(process.argv);
  if (wanted.length === 0) {
    console.error('give at least one --model, or a --from-file list');
    process.exitCode = 2;
    return;
  }

  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY (a workspace key; this script only ' +
                  'sends GET requests)');
    process.exitCode = 2;
    return;
  }

  const today = new Date(`${new Date().toISOString().slice(0, 10)}T00:00:00Z`);
  let unpinned = 0;
  for (const modelId of wanted) {
    const [state, detail] = verdict(modelId, await getModel(key, modelId), today);
    const line = `${state.padEnd(15)} ${modelId}  ${detail}`;
    if (!BAD.includes(state)) { console.log(line); continue; }
    if (state === 'alias') unpinned += 1;
    console.warn(line);
    if (state === 'alias') {
      console.warn('  repair: write the resolved snapshot into the config in ' +
        "place of the alias, record today's mapping beside your eval results, " +
        "then check the new id's retirement date");
    }
  }

  console.log(`${wanted.length} id(s) checked, ${unpinned} unpinned alias(es)`);
  process.exitCode = unpinned ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
