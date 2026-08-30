/**
 * Align a collapse in cache-read share with the day a new model id appeared.
 *
 * Read only. One GET against the Admin API, which needs an Admin API key
 * (sk-ant-admin...). A workspace key is rejected by /v1/organizations/.
 *
 * Caches are keyed per model, so the first day on a new model is cold by
 * definition and a note that fires on it is wrong. This finds the single
 * largest step down in the daily cache-read share anywhere in the window and
 * asks whether it sits where the new model id first appears. A collapse three
 * weeks either side of the switch is something else, and this says so rather
 * than taking the credit.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

/**
 * Published minimum cacheable prompt length per model family, in tokens. Used
 * only to explain a confirmed step, never to make one.
 */
export const CACHE_MINIMUMS = {
  'claude-opus-5': 512,
  'claude-fable-5': 512,
  'claude-mythos-5': 512,
  'claude-mythos-preview': 2048,
  'claude-opus-4-8': 1024,
  'claude-opus-4-7': 2048,
  'claude-opus-4-6': 4096,
  'claude-opus-4-5': 4096,
  'claude-opus-4-1': 1024,
  'claude-opus-4': 1024,
  'claude-sonnet-5': 1024,
  'claude-sonnet-4-6': 1024,
  'claude-sonnet-4-5': 1024,
  'claude-sonnet-4': 1024,
  'claude-haiku-4-5': 4096,
  'claude-haiku-3-5': 2048,
};

const FINDINGS = new Set(['collapsed-after-model-change']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** The model's minimum cacheable prompt length. Pure. Null if unrecognised. */
export function cacheMinimum(model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return null;
  let best = null;
  for (const [family, floor] of Object.entries(CACHE_MINIMUMS)) {
    if (name === family || name.startsWith(`${family}-`)) {
      if (best === null || family.length > best[0].length) best = [family, floor];
    }
  }
  return best ? best[1] : null;
}

/** Normalise a timestamp to a UTC day. Pure. Null if unreadable. */
export function dayKey(stamp) {
  if (typeof stamp === 'boolean') return null;
  if (typeof stamp === 'number' && Number.isFinite(stamp)) {
    const when = new Date(Math.trunc(stamp) * 1000);
    if (Number.isNaN(when.getTime())) return null;
    return when.toISOString().slice(0, 10);
  }
  const text = String(stamp ?? '').trim().replace(' ', 'T');
  if (text.length < 10) return null;
  const head = text.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(head)) return null;
  return head;
}

/**
 * One row per day that carried input, sorted. Pure.
 * Days with no traffic are left out rather than zero-filled: an invented
 * zero-share weekend would be the largest step in most windows.
 */
export function dailyRows(buckets) {
  const merged = new Map();
  for (const bucket of buckets ?? []) {
    const day = dayKey(bucket?.starting_at ?? bucket?.start_time);
    if (day === null) continue;
    for (const result of bucket?.results ?? []) {
      if (!result || typeof result !== 'object') continue;
      const model = String(result.model ?? 'unknown');
      const creation = result.cache_creation ?? {};
      if (!merged.has(day)) {
        merged.set(day, { day, uncached: 0, reads: 0, writes: 0, byModel: {} });
      }
      const row = merged.get(day);
      const uncached = readInt(result.uncached_input_tokens);
      const reads = readInt(result.cache_read_input_tokens);
      const writes = readInt(creation.ephemeral_5m_input_tokens)
        + readInt(creation.ephemeral_1h_input_tokens);
      row.uncached += uncached;
      row.reads += reads;
      row.writes += writes;
      row.byModel[model] = (row.byModel[model] ?? 0) + uncached + reads + writes;
    }
  }
  const rows = [...merged.values()].filter((r) => r.uncached + r.reads > 0);
  rows.sort((a, b) => a.day.localeCompare(b.day));
  rows.forEach((row, position) => {
    row.position = position;
    row.share = row.reads / (row.reads + row.uncached);
  });
  return rows;
}

/**
 * Models that first appear after the window opens. Pure.
 * A model present on day one might have been running for a year, so its
 * arrival is an artefact of where the window starts.
 */
export function arrivalPositions(rows) {
  const first = new Map();
  for (const row of rows ?? []) {
    for (const model of Object.keys(row?.byModel ?? {})) {
      if (!first.has(model)) first.set(model, readInt(row?.position));
    }
  }
  const out = new Map();
  for (const [model, position] of first) if (position > 0) out.set(model, position);
  return out;
}

/**
 * Fraction of input on one model from a position onward. Pure. Null if idle.
 * The guard against blaming a model nobody uses: a canary on one percent of
 * traffic cannot move an organization-wide ratio.
 */
export function inputShareAfter(rows, model, position) {
  let total = 0;
  let mine = 0;
  for (const row of rows ?? []) {
    if (readInt(row?.position) < position) continue;
    for (const [name, tokens] of Object.entries(row?.byModel ?? {})) {
      total += readInt(tokens);
      if (name === model) mine += readInt(tokens);
    }
  }
  if (total <= 0) return null;
  return mine / total;
}

/**
 * The step across one position, with that day itself left out. Pure.
 * Excluding the day is the whole care here: a new model's first day is cold
 * because the cache is empty, which is correct behaviour.
 */
export function stepAt(shares, position, minSide = 3) {
  const all = [...(shares ?? [])];
  const before = all.slice(0, position);
  const after = all.slice(position + 1);
  if (before.length < minSide || after.length < minSide) return [null, null, null];
  const b = before.reduce((s, v) => s + v, 0) / before.length;
  const a = after.reduce((s, v) => s + v, 0) / after.length;
  return [b, a, b - a];
}

/**
 * The largest downward step anywhere in the series. Pure.
 * What makes the alignment claim falsifiable: without it, any window holding
 * both a new model and a decline reads as causation.
 */
export function bestSplit(shares, minSide = 3) {
  const all = [...(shares ?? [])];
  const n = all.length;
  if (n < minSide * 2) return [null, null];
  let bestPosition = null;
  let bestDelta = null;
  for (let position = minSide; position <= n - minSide; position += 1) {
    const b = all.slice(0, position).reduce((s, v) => s + v, 0) / position;
    const a = all.slice(position).reduce((s, v) => s + v, 0) / (n - position);
    const delta = b - a;
    if (bestDelta === null || delta > bestDelta) {
      bestPosition = position;
      bestDelta = delta;
    }
  }
  return [bestPosition, bestDelta];
}

/** True when every day after the switch sits below every day before. Pure. */
export function sustained(shares, position, minSide = 3) {
  const all = [...(shares ?? [])];
  const before = all.slice(0, position);
  const after = all.slice(position + 1);
  if (before.length < minSide || after.length < minSide) return false;
  return Math.max(...after) < Math.min(...before);
}

/** Why the share might not come back, when the floors explain it. Pure. */
export function floorNote(oldModel, newModel) {
  const oldFloor = cacheMinimum(oldModel);
  const newFloor = cacheMinimum(newModel);
  if (oldFloor === null || newFloor === null) return '';
  if (newFloor > oldFloor) {
    return `${newModel} needs ${newFloor} tokens before a prefix is cacheable `
      + `and ${oldModel} needed ${oldFloor}, so a prompt that has not changed `
      + 'can have stopped qualifying. That is the '
      + 'prompt-below-model-cache-minimum note, and it is the most likely '
      + 'mechanism here.';
  }
  return `${newModel} has the same or a lower cache minimum (${newFloor}) as `
    + `${oldModel} (${oldFloor}), so the floor does not explain this. Look at `
    + 'thinking or effort defaults and at the tokenizer instead.';
}

/** Which note owns this shape, when it is not this one. Pure. */
export function handoff(state) {
  if (state === 'no-new-model') {
    return 'no model id appeared for the first time in this window, so nothing '
      + 'here can be attributed to a switch. If the share is low, read '
      + 'cache-invalidated-by-changing-prefix and prompt-caching-never-used.';
  }
  if (state === 'step-elsewhere') {
    return 'the largest step in the series is not where the new model arrived, '
      + 'so something else changed on that day. Read the '
      + 'cache-invalidated-by-changing-prefix note and line the step up against '
      + 'your deploys.';
  }
  if (state === 'expected-cold-start') {
    return 'the share dropped on the switch day and came back. That is a cold '
      + 'cache filling up, which is what a model change is supposed to cost, '
      + 'and it is not a finding.';
  }
  return '';
}

/** Classify one window. Pure. Returns [state, detail]. */
export function classify(rows, minDays = 14, minDrop = 0.15, ratioFloor = 0.6,
                         minMigration = 0.20, minSide = 3) {
  const all = rows ?? [];
  if (all.length < minDays) {
    return ['too-few-days',
      `${all.length} day(s) with input in the window, under the floor of ${minDays}`];
  }

  const shares = all.map((r) => r.share);
  const arrivals = arrivalPositions(all);
  if (arrivals.size === 0) {
    return ['no-new-model', 'every model id in this window was already present on day one'];
  }

  const ranked = [...arrivals.entries()].sort(
    (a, b) => (inputShareAfter(all, b[0], b[1]) ?? 0) - (inputShareAfter(all, a[0], a[1]) ?? 0));
  const [model, position] = ranked[0];
  const migration = inputShareAfter(all, model, position) ?? 0;
  if (migration < minMigration) {
    return ['new-model-marginal',
      `${model} arrived on ${all[position].day} but carries only `
      + `${(migration * 100).toFixed(0)}% of input since, under the floor of `
      + `${(minMigration * 100).toFixed(0)}%. Too small to move the ratio.`];
  }

  const [before, after, delta] = stepAt(shares, position, minSide);
  if (delta === null) {
    return ['window-too-short-around-the-switch',
      `${model} arrived on ${all[position].day} with fewer than ${minSide} day(s) `
      + 'either side of it'];
  }

  // Alignment before magnitude, and only against a step that is material on
  // its own: a big fall elsewhere disqualifies the switch outright.
  const [peak, peakDelta] = bestSplit(shares, minSide);
  if (peak !== null && peakDelta !== null && peakDelta >= minDrop
      && Math.abs(peak - position) > 1) {
    return ['step-elsewhere',
      `the share falls hardest at ${all[peak].day}, not at the ${model} switch `
      + `on ${all[position].day}`];
  }

  if (delta < minDrop || after > before * ratioFloor) {
    if (before - shares[position] >= minDrop) {
      return ['expected-cold-start',
        `${model} arrived on ${all[position].day}, the share dipped to `
        + `${(shares[position] * 100).toFixed(0)}% that day and settled back at `
        + `${(after * 100).toFixed(0)}% against ${(before * 100).toFixed(0)}% before`];
    }
    return ['steady',
      `${model} arrived on ${all[position].day} and the share held at `
      + `${(after * 100).toFixed(0)}% against ${(before * 100).toFixed(0)}% before`];
  }

  if (peak === null || Math.abs(peak - position) > 1) {
    return ['step-elsewhere',
      `the share falls hardest at ${peak === null ? 'no single day' : all[peak].day}, `
      + `not at the ${model} switch on ${all[position].day}`];
  }

  if (!sustained(shares, position, minSide)) {
    return ['partial-recovery',
      `${(before * 100).toFixed(0)}% before the ${model} switch and `
      + `${(after * 100).toFixed(0)}% after, but some days since have recovered `
      + 'above the pre-switch floor. Suggestive and not conclusive: widen the window.'];
  }

  return ['collapsed-after-model-change',
    `cache-read share ${(before * 100).toFixed(0)}% before ${model} arrived on `
    + `${all[position].day} and ${(after * 100).toFixed(0)}% after, with the `
    + `switch day itself excluded. ${model} now carries `
    + `${(migration * 100).toFixed(0)}% of input and the largest step in the `
    + 'window is exactly there.'];
}

/** The model carrying the most input before the switch. Pure. */
export function previousModel(rows, position) {
  const totals = new Map();
  for (const row of rows ?? []) {
    if (readInt(row?.position) >= position) continue;
    for (const [name, tokens] of Object.entries(row?.byModel ?? {})) {
      totals.set(name, (totals.get(name) ?? 0) + readInt(tokens));
    }
  }
  if (totals.size === 0) return null;
  return [...totals.entries()].reduce((a, b) => (a[1] >= b[1] ? a : b))[0];
}

/** What to check about the new model, in the order that pays. Pure. */
export function repairLines(oldModel, newModel) {
  const lines = [];
  const note = floorNote(oldModel, newModel);
  if (note) lines.push(note);
  lines.push(
    "compare the two models' minimum cacheable token counts and move the "
    + 'cache_control breakpoint so the prefix clears the higher one.',
    'compare their thinking and effort defaults. Those are model-specific and '
    + 'they sit inside the cached prefix, so a different default is a different prefix.',
    'count the prefix again under the new model id. A newer tokenizer can '
    + 'produce materially more tokens for the same text, which moves a prefix '
    + 'that used to sit just above a boundary.',
    'then re-measure the cache-read share over the following three days, not '
    + 'the following one. The first day after any breakpoint change is cold for '
    + 'the same reason the switch day was.',
  );
  return lines;
}

function windowStart(days) {
  const now = new Date();
  now.setUTCHours(0, 0, 0, 0);
  return `${new Date(now.getTime() - days * 86400000).toISOString().slice(0, 19)}Z`;
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/ needs an `
                    + 'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* readBuckets(key, path, params) {
  let query = { ...params };
  for (;;) {
    const page = await get(key, path, query);
    for (const bucket of page?.data ?? []) yield bucket;
    if (!page?.has_more || !page?.next_page) return;
    query = { ...params, page: page.next_page };
  }
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); '
                  + 'a workspace key cannot read /v1/organizations/');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(14, Math.min(Number((process.env.DAYS || "dummy-days") ?? 31), 90));
  const minDrop = Number((process.env.MIN_DROP || "dummy-min-drop") ?? 0.15);

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organizations/usage_report/messages', {
    starting_at: windowStart(days),
    bucket_width: '1d',
    limit: days + 1,
    'group_by[]': ['model'],
  })) buckets.push(bucket);

  const rows = dailyRows(buckets);
  if (rows.length === 0) {
    console.log(`no messages usage in the last ${days} day(s)`);
    return;
  }

  const [state, detail] = classify(rows, 14, minDrop);
  const line = `${state.padEnd(32)} ${detail}`;

  if (FINDINGS.has(state)) {
    console.warn(line);
    const arrivals = arrivalPositions(rows);
    const ranked = [...arrivals.entries()].sort(
      (a, b) => (inputShareAfter(rows, b[0], b[1]) ?? 0)
              - (inputShareAfter(rows, a[0], a[1]) ?? 0));
    const [model, position] = ranked[0];
    for (const repair of repairLines(previousModel(rows, position), model)) {
      console.warn(`  repair: ${repair}`);
    }
    console.warn('  note: this is an organization-wide ratio. A second workload '
                 + 'that changed on the same day would be folded into it, so '
                 + 'line the date up against a deploy before acting.');
    console.log(`${rows.length} day(s) checked, 1 finding(s)`);
    process.exitCode = 1;
    return;
  }

  const note = handoff(state);
  console.log(line);
  if (note) console.log(`  ${note}`);
  console.log(`${rows.length} day(s) checked, 0 finding(s)`);
  process.exitCode = 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
