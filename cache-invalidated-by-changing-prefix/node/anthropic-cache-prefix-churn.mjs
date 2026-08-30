/**
 * Find Anthropic keys whose cache is rewritten on every call and never read.
 *
 * Read only. One GET against the Admin API, which needs an Admin API key
 * (sk-ant-admin...). A workspace key is rejected by /v1/organizations/.
 *
 * Totals cannot separate this from two neighbouring problems, so the evidence
 * is spacing: a run of adjacent one-minute buckets that each write and never
 * read is longer than the entry's TTL, so the entry was alive and unmatched.
 * Caching switched off, and caching read but not read enough, are named and
 * handed to their own notes.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const FINDINGS = new Set(['prefix-churn']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** Normalise a timestamp to a UTC minute key. Pure. Null if unreadable. */
export function minuteKey(stamp) {
  if (typeof stamp === 'boolean') return null;
  if (typeof stamp === 'number' && Number.isFinite(stamp)) {
    const when = new Date(Math.trunc(stamp) * 1000);
    if (Number.isNaN(when.getTime())) return null;
    return `${when.toISOString().slice(0, 16)}Z`;
  }
  const text = String(stamp ?? '').trim().replace(' ', 'T');
  if (text.length < 16) return null;
  const head = text.slice(0, 16);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(head)) return null;
  return `${head}Z`;
}

/**
 * Minutes since the epoch. Pure. Null if unreadable.
 * Adjacency has to be integer arithmetic: string comparison puts 14:59 and
 * 15:00 two apart and quietly halves every run that crosses an hour.
 */
export function minuteIndex(stamp) {
  const key = minuteKey(stamp);
  if (key === null) return null;
  const when = Date.parse(`${key.slice(0, 16)}:00Z`);
  if (Number.isNaN(when)) return null;
  return Math.floor(when / 60000);
}

/** Per api_key_id and model, one row per minute, sorted. Pure. */
export function rowsByKey(buckets) {
  const merged = new Map();
  for (const bucket of buckets ?? []) {
    const stamp = bucket?.starting_at ?? bucket?.start_time;
    const key = minuteKey(stamp);
    const index = minuteIndex(stamp);
    if (key === null || index === null) continue;
    for (const result of bucket?.results ?? []) {
      if (!result || typeof result !== 'object') continue;
      const ident = `${result.api_key_id ?? 'unknown'}\t${result.model ?? 'unknown'}`;
      const cell = `${ident}\t${index}`;
      if (!merged.has(cell)) {
        merged.set(cell, { ident, minute: key, index, uncached: 0,
                           write5m: 0, write1h: 0, reads: 0 });
      }
      const row = merged.get(cell);
      const creation = result.cache_creation ?? {};
      row.uncached += readInt(result.uncached_input_tokens);
      row.write5m += readInt(creation.ephemeral_5m_input_tokens);
      row.write1h += readInt(creation.ephemeral_1h_input_tokens);
      row.reads += readInt(result.cache_read_input_tokens);
    }
  }
  const out = new Map();
  for (const row of merged.values()) {
    if (!out.has(row.ident)) out.set(row.ident, []);
    out.get(row.ident).push(row);
  }
  for (const rows of out.values()) rows.sort((a, b) => a.index - b.index);
  return out;
}

/** Cache creation tokens in one minute, both TTLs. Pure. */
export function writes(row) {
  return readInt(row?.write5m) + readInt(row?.write1h);
}

/** Share of a minute's input written as a fresh entry. Pure. Null when idle. */
export function writeShare(row) {
  const total = readInt(row?.uncached) + writes(row);
  if (total <= 0) return null;
  return writes(row) / total;
}

/** Sum a series, and count the minutes that carried any traffic. Pure. */
export function totals(rows) {
  const out = { uncached: 0, write5m: 0, write1h: 0, reads: 0, active: 0 };
  for (const row of rows ?? []) {
    out.uncached += readInt(row?.uncached);
    out.write5m += readInt(row?.write5m);
    out.write1h += readInt(row?.write1h);
    out.reads += readInt(row?.reads);
    if (readInt(row?.uncached) + writes(row) + readInt(row?.reads) > 0) out.active += 1;
  }
  out.writes = out.write5m + out.write1h;
  return out;
}

/**
 * Maximal runs of adjacent minutes that wrote and never read. Pure.
 * The finding. A 5 minute entry written at the start of a five minute run was
 * still alive at the end of it, so nothing but a moving prefix explains a run.
 */
export function churnRuns(rows, shareFloor = 0.5, readFloor = 0.01) {
  const runs = [];
  let current = [];
  for (const row of rows ?? []) {
    const made = writes(row);
    const share = writeShare(row);
    const churning = made > 0 && share !== null && share >= shareFloor
      && readInt(row?.reads) <= made * readFloor;
    if (!churning) {
      if (current.length > 0) { runs.push(current); current = []; }
      continue;
    }
    if (current.length > 0 && readInt(row?.index) === readInt(current[current.length - 1]?.index) + 1) {
      current.push(row);
    } else {
      if (current.length > 0) runs.push(current);
      current = [row];
    }
  }
  if (current.length > 0) runs.push(current);
  return runs;
}

/**
 * Median gap in minutes between minutes that wrote. Pure. Null under two.
 * The alternative explanation as a number: traffic slower than the TTL writes
 * isolated entries that expire before anything can read them.
 */
export function gapProfile(rows) {
  const indices = (rows ?? []).filter((r) => writes(r) > 0)
    .map((r) => readInt(r?.index)).sort((a, b) => a - b);
  if (indices.length < 2) return null;
  const gaps = [];
  for (let i = 0; i < indices.length - 1; i += 1) gaps.push(indices[i + 1] - indices[i]);
  gaps.sort((a, b) => a - b);
  const middle = Math.floor(gaps.length / 2);
  if (gaps.length % 2) return gaps[middle];
  return (gaps[middle - 1] + gaps[middle]) / 2;
}

/** Which TTL the writes were bought at. Pure. Returns [state, detail]. */
export function ttlSplit(sums) {
  const five = readInt(sums?.write5m);
  const hour = readInt(sums?.write1h);
  if (five + hour <= 0) {
    return ['no-writes', 'nothing was written to the cache in this window'];
  }
  if (hour > five) {
    return ['1h-dominant',
      'the writes are mostly 1 hour entries at 2x base input, so each one was ' +
      'alive for sixty minutes and never matched in any of them'];
  }
  if (five > hour) {
    return ['5m-dominant',
      'the writes are 5 minute entries at 1.25x base input, so any run longer ' +
      'than five minutes outlived calls that never matched it'];
  }
  return ['mixed', 'the writes are split evenly between the 5 minute and 1 hour TTLs'];
}

/** Which note owns this shape, when it is not this one. Pure. */
export function handoff(state) {
  if (state === 'caching-off') {
    return 'no writes and no reads anywhere: caching was never switched on for ' +
      'this key. Read the prompt-caching-never-used note; the loss there is a ' +
      'discount not taken rather than a surcharge paid.';
  }
  if (state === 'cache-is-read') {
    return 'entries are being matched, so the prefix is stable enough to hit. ' +
      'Whether it hits often enough to pay for the write premium is the ' +
      'write-to-read ratio, which is the cache-writes-with-no-reads note.';
  }
  if (state === 'gap-driven-misses') {
    return 'the writing minutes are isolated rather than adjacent, so each ' +
      'entry plausibly expired before the next call arrived. That is arrival ' +
      'rate against TTL, and it is the cache-writes-with-no-reads note rather ' +
      'than this one.';
  }
  return '';
}

/** Classify one key and model series. Pure. Returns [state, detail]. */
export function classify(rows, minRun = 5, shareFloor = 0.5, readFloor = 0.01,
                         minActive = 10) {
  const sums = totals(rows);
  if (sums.active < minActive) {
    return ['too-little-traffic',
      `${sums.active} active minute(s), under the floor of ${minActive}. ` +
      'Nothing can be said about spacing with fewer.'];
  }

  if (sums.writes === 0 && sums.reads === 0) {
    return ['caching-off',
      `${sums.uncached} uncached input token(s), no cache writes and no cache reads`];
  }
  if (sums.writes === 0) {
    return ['reads-only',
      `${sums.reads} cache read(s) and no writes in this window: the entries ` +
      'were written before it started'];
  }
  if (sums.reads > sums.writes * readFloor) {
    return ['cache-is-read',
      `${sums.reads} cache read token(s) against ${sums.writes} written`];
  }

  const share = sums.writes / (sums.uncached + sums.writes);
  if (share < shareFloor) {
    return ['small-cached-prefix',
      `writes are ${(share * 100).toFixed(0)}% of input with reads at 0, under ` +
      `the floor of ${(shareFloor * 100).toFixed(0)}%. Something is being ` +
      'cached and never matched, and it is a minority of the prompt rather ' +
      'than the prefix.'];
  }

  const runs = churnRuns(rows, shareFloor, readFloor);
  let longest = [];
  for (const run of runs) if (run.length > longest.length) longest = run;
  if (longest.length >= minRun) {
    return ['prefix-churn',
      `writes are ${(share * 100).toFixed(0)}% of input with reads at 0; ` +
      `longest run ${longest.length} adjacent minute(s) from ` +
      `${longest[0].minute} to ${longest[longest.length - 1].minute}. The ` +
      'entry written at the start of that run was still alive at the end and ' +
      'was never matched, so the prefix differs on every call.'];
  }

  const gap = gapProfile(rows);
  if (gap !== null && gap > minRun) {
    return ['gap-driven-misses',
      `writes are ${(share * 100).toFixed(0)}% of input with reads at 0, and ` +
      `the writing minutes sit a median of ${gap.toFixed(0)} minute(s) apart`];
  }

  return ['intermittent-misses',
    `writes are ${(share * 100).toFixed(0)}% of input with reads at 0, and the ` +
    `longest run of adjacent writing minutes is ${longest.length}, under the ` +
    `floor of ${minRun}. Suggestive and not conclusive: widen the window.`];
}

/** The invalidator hunt, in cache order. Pure. */
export function repairLines() {
  return [
    'hunt the invalidator in cache order: tools, then system, then messages. ' +
    'A change to the tools invalidates all three.',
    'the usual suspects are a clock (datetime.now in a system prompt), a tool ' +
    'list built from an unordered dict, a per-request id, a per-user preamble ' +
    'placed before the breakpoint, and an option toggled per call such as ' +
    'tool_choice, citations, web search or reasoning effort.',
    'move each one strictly after the last cache_control breakpoint, then ' +
    're-read these same minute buckets. The runs should break up before the ' +
    'totals move.',
  ];
}

function windowStart(minutes) {
  const now = new Date();
  now.setUTCSeconds(0, 0);
  return `${new Date(now.getTime() - minutes * 60000).toISOString().slice(0, 19)}Z`;
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
    throw new Error(`${res.status} from Anthropic: /v1/organizations/ needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
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
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/');
    process.exitCode = 2;
    return;
  }
  const minutes = Math.max(30, Math.min(Number((process.env.MINUTES || "dummy-minutes") ?? 240), 1440));
  const minRun = Number((process.env.MIN_RUN || "dummy-min-run") ?? 5);
  const shareFloor = Number((process.env.SHARE_FLOOR || "dummy-share-floor") ?? 0.5);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of readBuckets(admin, '/organizations/usage_report/messages', {
    starting_at: windowStart(minutes),
    bucket_width: '1m',
    limit: minutes,
    'group_by[]': ['api_key_id', 'model'],
  })) buckets.push(bucket);

  const series = rowsByKey(buckets);
  if (series.size === 0) {
    console.log(`no messages usage in the last ${minutes} minute(s)`);
    return;
  }

  let checked = 0;
  let bad = 0;
  for (const ident of [...series.keys()].sort()) {
    const rows = series.get(ident);
    const [state, detail] = classify(rows, minRun, shareFloor);
    checked += 1;
    const line = `${state.padEnd(20)} ${ident.replace('\t', ' / ')}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      const [, ttl] = ttlSplit(totals(rows));
      console.warn(`  ${ttl}`);
      console.warn('  note: grouped by key and model. A key serving many tenants ' +
                   'with a per tenant prefix writes constantly and correctly; ' +
                   'this finding is strongest on a key with one workload.');
      for (const repair of repairLines()) console.warn(`  repair: ${repair}`);
    } else {
      const note = handoff(state);
      if (note) {
        console.log(line);
        console.log(`  ${note}`);
      } else if (showAll || state === 'intermittent-misses') {
        console.log(line);
      }
    }
  }

  console.log(`${checked} key/model series checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
