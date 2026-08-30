/**
 * Find retired Claude model ids still named in your configuration.
 *
 * Read only. GET requests and nothing else: give this a workspace API key. The
 * repair is printed, never performed.
 */
import { readFileSync } from 'node:fs';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// Copied from the published deprecations page, because the API has no
// retirement field at all. A hardcoded table goes stale, so the live list from
// the API always wins over this one; see verdict().
export const RETIRED = {
  'claude-opus-4-1-20250805': '2026-08-05',
  'claude-opus-4-20250514': '2026-06-15',
  'claude-sonnet-4-20250514': '2026-06-15',
  'claude-3-haiku-20240307': '2026-04-20',
  'claude-3-7-sonnet-20250219': '2026-02-19',
  'claude-3-5-haiku-20241022': '2026-02-19',
  'claude-3-opus-20240229': '2026-01-05',
  'claude-3-5-sonnet-20240620': '2025-10-28',
  'claude-3-5-sonnet-20241022': '2025-10-28',
  'claude-3-sonnet-20240229': '2025-07-21',
  'claude-2.0': '2025-07-21',
  'claude-2.1': '2025-07-21',
  'claude-1.0': '2024-11-06',
  'claude-1.1': '2024-11-06',
  'claude-1.2': '2024-11-06',
  'claude-1.3': '2024-11-06',
  'claude-instant-1.0': '2024-11-06',
  'claude-instant-1.1': '2024-11-06',
  'claude-instant-1.2': '2024-11-06',
};

const BAD = ['retired', 'unknown', 'table-stale', 'unreadable'];
const DAY = 86400000;

/**
 * Where a retired line rolls forward to, by family. Family level on purpose:
 * this says the Opus line continues as Opus, not that any two snapshots behave
 * the same.
 */
export function replacement(modelId) {
  if (modelId.includes('opus')) return 'claude-opus-4-8';
  if (modelId.includes('haiku') || modelId.includes('instant')) {
    return 'claude-haiku-4-5-20251001';
  }
  if (modelId.includes('sonnet') || /^claude-[12]/.test(modelId)) {
    return 'claude-sonnet-4-6';
  }
  return null;
}

/** Whole days from a YYYY-MM-DD string to `today`, or null if unreadable. */
export function daysSince(dayStr, today) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(dayStr))) return null;
  const ms = Date.parse(`${dayStr}T00:00:00Z`);
  if (Number.isNaN(ms)) return null;
  return Math.round((today.getTime() - ms) / DAY);
}

/**
 * Classify one model string against the live list and the retirement table.
 * Pure: both the live set and the date come in as arguments. Returns
 * [state, detail].
 *
 * The live list wins over the table. If the API still lists an id the table
 * calls retired, the table is out of date, not the API.
 */
export function verdict(modelId, liveIds, today) {
  const id = String(modelId ?? '').trim();
  if (!id) return ['unreadable', 'empty model string'];

  const retiredOn = RETIRED[id];

  if (liveIds.has(id)) {
    if (retiredOn) {
      return ['table-stale',
        `still in the live models list, though the local table says it retired ` +
        `on ${retiredOn}. Trust the API and correct the table.`];
    }
    return ['live', 'in the live models list for this workspace'];
  }

  if (retiredOn) {
    const ago = daysSince(retiredOn, today);
    const when = ago === null ? retiredOn : `${retiredOn}, ${ago} day(s) ago`;
    const movedTo = replacement(id);
    return ['retired',
      `retired on ${when}. Every request naming it returns 404 not_found_error, ` +
      `the same body a mistyped id returns.` +
      (movedTo ? ` Line continues as ${movedTo}.` : '')];
  }

  return ['unknown',
    'not in the live list and not on the deprecation table. That is a typo, an ' +
    'id that only exists on Bedrock or Vertex (which run later retirement ' +
    'schedules), or a model this workspace has not been granted. Three ' +
    'different repairs, so check before assuming.'];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: check ANTHROPIC_API_KEY; an ` +
                    'Admin key cannot read the models list');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

export async function liveModelIds(key) {
  const ids = new Set();
  const params = { limit: 1000 };
  for (;;) {
    const page = await get(key, '/models', params);
    for (const m of page.data ?? []) if (m.id) ids.add(String(m.id));
    if (!page.has_more || !page.last_id) break;
    params.after_id = page.last_id;
  }
  return ids;
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
    console.error("give at least one --model, or a --from-file list. Collect " +
                  "them with: grep -rn 'claude-' .");
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

  const live = await liveModelIds(key);
  const today = new Date(`${new Date().toISOString().slice(0, 10)}T00:00:00Z`);

  const counts = new Map();
  let bad = 0;
  for (const modelId of wanted) {
    const [state, detail] = verdict(modelId, live, today);
    counts.set(state, (counts.get(state) ?? 0) + 1);
    const line = `${state.padEnd(12)} ${modelId || '<empty>'}  ${detail}`;
    if (!BAD.includes(state)) { console.log(line); continue; }
    bad += 1;
    console.warn(line);
    if (state === 'retired') {
      const movedTo = replacement(modelId) ?? 'the documented replacement';
      console.warn(`  repair: replace the string "${modelId}" with "${movedTo}" ` +
        'everywhere it appears, including default arguments, fallback branches ' +
        'and batch request bodies');
    }
  }

  console.log(`${wanted.length} id(s) checked against ${live.size} live ` +
    `model(s), ${counts.get('retired') ?? 0} retired, ` +
    `${counts.get('unknown') ?? 0} unknown`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
