/**
 * Find OpenAI traffic that runs cold in the hours that follow a gap.
 *
 * Read only. One paginated GET against the Usage API, which needs an admin key
 * (sk-admin-...). A project key is rejected by /v1/organization/.
 *
 * Cached prefixes are evicted after an idle period and the default window is
 * short, so a nightly batch or a cron job starts cold every time on a prefix
 * that has not changed in months. The signature is positional rather than
 * arithmetic: the cold hours are the ones that resume after a gap, and the
 * hours that follow a busy hour are fine.
 *
 * The finding is the shortest gap length at which the share has already
 * collapsed, because that number and the retention setting are the same
 * number, and it says whether the repair is a parameter or a schedule.
 */
const API = 'https://api.openai.com/v1';

/**
 * Gap lengths in hours, coarse enough that each band maps onto a different
 * repair. Shortest first: the finding is the first band that is already cold.
 */
export const BIN_ORDER = ['1h', '2-5h', '6-23h', '24h+'];

const FINDINGS = new Set(['cold-after-idle']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Hours since the epoch. Pure. Null if unreadable.
 * Gaps have to be integer arithmetic: counting idle hours by comparing
 * formatted stamps gets 23:00 and 00:00 wrong every night, and a nightly job
 * is exactly the workload this note is about.
 */
export function hourIndex(stamp) {
  if (typeof stamp === 'boolean' || stamp === null || stamp === undefined) return null;
  if (typeof stamp === 'number' && Number.isFinite(stamp)) {
    return Math.floor(Math.trunc(stamp) / 3600);
  }
  const text = String(stamp).trim().replace(' ', 'T');
  if (text.length < 13) return null;
  const head = text.slice(0, 13);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}$/.test(head)) return null;
  const when = Date.parse(`${head}:00:00Z`);
  if (Number.isNaN(when)) return null;
  return Math.floor(when / 3600000);
}

/** Render an hour index back as a UTC stamp. Pure. */
export function hourLabel(index) {
  if (index === null || index === undefined) return 'unknown';
  return `${new Date(Math.trunc(index) * 3600000).toISOString().slice(0, 13)}:00Z`;
}

/**
 * Per project_id and model, one row per active hour, sorted. Pure.
 * Only hours that carried traffic become rows, and they must not be
 * zero-filled: the gap is the distance between two rows.
 */
export function rowsBySeries(buckets) {
  const merged = new Map();
  for (const bucket of buckets ?? []) {
    const index = hourIndex(bucket?.start_time);
    if (index === null) continue;
    for (const result of bucket?.results ?? []) {
      if (!result || typeof result !== 'object') continue;
      const ident = `${result.project_id ?? 'unknown'}\t${result.model ?? 'unknown'}`;
      const cell = `${ident}\t${index}`;
      if (!merged.has(cell)) {
        merged.set(cell, { ident, index, hour: hourLabel(index),
                           requests: 0, input: 0, cached: 0 });
      }
      const row = merged.get(cell);
      row.requests += readInt(result.num_model_requests);
      row.input += readInt(result.input_tokens);
      row.cached += readInt(result.input_cached_tokens);
    }
  }
  const out = new Map();
  for (const row of merged.values()) {
    if (row.requests <= 0 && row.input <= 0) continue;
    if (!out.has(row.ident)) out.set(row.ident, []);
    out.get(row.ident).push(row);
  }
  for (const rows of out.values()) rows.sort((a, b) => a.index - b.index);
  return out;
}

/** Pooled cached share over a set of hours. Pure. Null when nothing ran. */
export function cachedShare(rows) {
  let input = 0;
  let cached = 0;
  for (const row of rows ?? []) {
    input += readInt(row?.input);
    cached += readInt(row?.cached);
  }
  if (input <= 0) return null;
  return cached / input;
}

/**
 * Annotate each hour with the idle hours immediately before it. Pure.
 * The first row is dropped rather than given a gap of zero: nothing is visible
 * before the window starts, and guessing either way biases the comparison this
 * note rests on.
 */
export function withGaps(rows) {
  const ordered = [...(rows ?? [])].sort((a, b) => readInt(a?.index) - readInt(b?.index));
  const out = [];
  let previous = null;
  for (const row of ordered) {
    const index = readInt(row?.index);
    if (previous !== null) out.push({ ...row, gap: index - previous - 1 });
    previous = index;
  }
  return out;
}

/** Bucket a gap length into the band its repair belongs to. Pure. */
export function gapBin(gap) {
  const n = readInt(gap);
  if (n <= 0) return 'continuous';
  if (n === 1) return '1h';
  if (n <= 5) return '2-5h';
  if (n <= 23) return '6-23h';
  return '24h+';
}

/**
 * Cached share per gap band. Pure. The finding's shape: everything else in the
 * section reads a ratio against time or against load, this reads it against
 * how long the traffic had been away.
 */
export function binShares(annotated) {
  const out = {};
  for (const row of annotated ?? []) {
    const band = gapBin(row?.gap);
    if (!out[band]) out[band] = { hours: 0, input: 0, cached: 0, share: null };
    out[band].hours += 1;
    out[band].input += readInt(row?.input);
    out[band].cached += readInt(row?.cached);
  }
  for (const cell of Object.values(out)) {
    cell.share = cell.input > 0 ? cell.cached / cell.input : null;
  }
  return out;
}

/**
 * The shortest gap at which the share has already gone. Pure. Null if none.
 * Shortest rather than worst on purpose: what decides the repair is whether
 * one idle hour was already enough, or whether it takes a day.
 */
export function collapseBin(bands, coldCeiling = 0.05, minHours = 3) {
  for (const band of BIN_ORDER) {
    const cell = (bands ?? {})[band];
    if (!cell || cell.share === null || cell.share === undefined) continue;
    if (cell.hours >= minHours && cell.share <= coldCeiling) return band;
  }
  return null;
}

/**
 * Tokens that would have been cached at the warm rate. Pure.
 * Priced against the share this same prefix achieves when traffic is
 * continuous, which is this workload's own best hour rather than a borrowed
 * target.
 */
export function foregoneTokens(bands, warmShare) {
  if (warmShare === null || warmShare === undefined) return 0;
  let total = 0;
  for (const band of BIN_ORDER) {
    const cell = (bands ?? {})[band];
    if (!cell || cell.share === null || cell.share === undefined) continue;
    total += Math.trunc(Math.max(0, warmShare - cell.share) * cell.input);
  }
  return total;
}

/** Which note owns this shape, when it is not this one. Pure. */
export function handoff(state) {
  if (state === 'never-idle') {
    return 'this series has no gaps at all, so eviction between runs cannot be '
      + 'the story. If the share is still low, read the prompt-cache-key-not-set '
      + 'note and check whether it degrades at peak instead.';
  }
  if (state === 'cold-everywhere') {
    return 'the continuously busy hours are cold too, so the prefix is not being '
      + 'matched even when the entry is certainly alive. Read '
      + 'cache-invalidated-by-changing-prefix, and '
      + 'prompt-below-model-cache-minimum if nothing caches at all.';
  }
  return '';
}

/** Classify one project and model series. Pure. Returns [state, detail]. */
export function classify(rows, coldCeiling = 0.05, warmFloor = 0.20,
                         minHours = 24, minBandHours = 3) {
  const annotated = withGaps(rows);
  if (annotated.length < minHours) {
    return ['too-few-hours',
      `${annotated.length} usable hour(s) after dropping the first, under the `
      + `floor of ${minHours}`];
  }

  const bands = binShares(annotated);
  const warm = bands.continuous ?? {};
  const warmShare = warm.share ?? null;
  let idleHours = 0;
  for (const [band, cell] of Object.entries(bands)) {
    if (band !== 'continuous') idleHours += cell.hours;
  }

  if (idleHours < minBandHours) {
    return ['never-idle',
      `${annotated.length} hour(s) of traffic and only ${idleHours} of them `
      + 'resume after a gap'];
  }

  if (warmShare === null || (warm.hours ?? 0) < minBandHours) {
    return ['no-continuous-hours',
      'traffic never runs two hours back to back, so there is no warm baseline '
      + 'to compare a resumption against'];
  }

  if (warmShare <= coldCeiling) {
    return ['cold-everywhere',
      `${(warmShare * 100).toFixed(0)}% cached even in continuously busy hours`];
  }

  if (warmShare < warmFloor) {
    return ['warm-baseline-too-weak',
      `${(warmShare * 100).toFixed(0)}% cached in continuously busy hours, under `
      + `the floor of ${(warmFloor * 100).toFixed(0)}%. The prefix is barely `
      + 'caching at the best of times, so the gaps are not the main story'];
  }

  const band = collapseBin(bands, coldCeiling, minBandHours);
  if (band === null) {
    return ['warm-after-idle',
      `${(warmShare * 100).toFixed(0)}% cached when continuous and no gap band has collapsed`];
  }

  const cell = bands[band];
  return ['cold-after-idle',
    `${(warmShare * 100).toFixed(0)}% cached in continuously busy hours and `
    + `${(cell.share * 100).toFixed(0)}% in the ${cell.hours} hour(s) that resume `
    + `after a gap of ${band}. The prefix is fine; the entry is evicted while `
    + 'nobody is calling.'];
}

/** The repair, keyed to how short a gap already loses the cache. Pure. */
export function repairLines(band, foregone) {
  const lines = [];
  if (band === '1h') {
    lines.push('a single idle hour is already enough, so no retention setting '
      + 'on offer covers it on its own: the 30m ttl expires inside the gap.');
  } else if (band === '2-5h' || band === '6-23h') {
    lines.push(`the cache survives a busy hour and not a gap of ${band}, which `
      + 'is the default retention window doing exactly what it says.');
  } else if (band === '24h+') {
    lines.push('gaps of a day or more, which is the one case the 24h retention '
      + 'option was added for.');
  }
  lines.push(
    'on models before GPT-5.6, set prompt_cache_retention="24h" on this route. '
    + 'It is opt-in and costs nothing extra to set.',
    'on GPT-5.6 and later, set prompt_cache_options={"ttl": "30m"} explicitly so '
    + 'the retention is visible in the code rather than inherited, then check it '
    + 'against your actual gap length.',
    'reshape the schedule. Run intermittent work in one contiguous window '
    + 'instead of scattering it across the day, so the first call warms an entry '
    + 'the rest of the batch reads.',
    `about ${foregone} input token(s) in this window would have been cached at `
    + "this workload's own continuous rate.",
  );
  return lines;
}

function windowStart(days) {
  const now = new Date();
  now.setUTCMinutes(0, 0, 0);
  return Math.floor((now.getTime() - days * 86400000) / 1000);
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/ needs an admin `
                    + 'key (sk-admin-...), not a project key');
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
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key '
                  + '(sk-admin-...); a project key cannot read /v1/organization/');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(2, Math.min(Number((process.env.DAYS || "dummy-days") ?? 14), 30));
  const coldCeiling = Number((process.env.COLD_CEILING || "dummy-cold-ceiling") ?? 0.05);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organization/usage/completions', {
    start_time: windowStart(days),
    bucket_width: '1h',
    limit: 168,
    'group_by[]': ['project_id', 'model'],
  })) buckets.push(bucket);

  const series = rowsBySeries(buckets);
  if (series.size === 0) {
    console.log(`no completions usage in the last ${days} day(s)`);
    return;
  }

  let checked = 0;
  let bad = 0;
  for (const ident of [...series.keys()].sort()) {
    const rows = series.get(ident);
    const [state, detail] = classify(rows, coldCeiling);
    checked += 1;
    const line = `${state.padEnd(24)} ${ident.replace('\t', ' / ')}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      const bands = binShares(withGaps(rows));
      const warm = bands.continuous?.share ?? null;
      const band = collapseBin(bands, coldCeiling);
      for (const name of BIN_ORDER) {
        const cell = bands[name];
        if (cell && cell.share !== null) {
          console.warn(`  after a gap of ${name.padEnd(5)} ${cell.hours} hour(s), `
                       + `${(cell.share * 100).toFixed(0)}% cached`);
        }
      }
      for (const repair of repairLines(band, foregoneTokens(bands, warm))) {
        console.warn(`  repair: ${repair}`);
      }
      console.warn('  note: hourly buckets cannot see an idle stretch shorter '
                   + 'than an hour, so a gap band of 1h is a ceiling on how '
                   + 'quickly the entry actually went.');
    } else {
      const note = handoff(state);
      if (note) {
        console.log(line);
        console.log(`  ${note}`);
      } else if (showAll) {
        console.log(line);
      }
    }
  }

  console.log(`${checked} project/model series checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
