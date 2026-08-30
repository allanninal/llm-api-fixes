/**
 * Report Claude workloads whose input has grown into the 200k-1M band.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path.
 *
 * A SIZE alarm and not a price alarm. On current models the 1M window is the
 * default, no beta header is involved, and long-context requests bill at
 * standard rates. What the band measures is a prefix that grows every turn.
 * The repair is compaction first, caching second, and it is printed.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const SHORT_BAND = '0-200k';
const LONG_BAND = '200k-1M';
const UNBANDED = 'unbanded';

const FINDINGS = ['long-context-uncached'];

/**
 * Normalise the context_window value. Pure.
 * A null becomes "unbanded", never "0-200k": folding unbanded traffic into the
 * short band deflates the long share and turns a real finding into a
 * comfortable number.
 */
export function band(result) {
  const raw = String(result?.context_window ?? '').trim().toLowerCase();
  if (raw === LONG_BAND.toLowerCase()) return LONG_BAND;
  if (raw === SHORT_BAND.toLowerCase()) return SHORT_BAND;
  return UNBANDED;
}

/** Sum input tokens into {model: {band: {uncached, cache_read}}}. Pure. */
export function fold(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const model = String(result.model ?? 'all models');
        const where = band(result);
        if (!out[model]) out[model] = {};
        if (!out[model][where]) out[model][where] = { uncached: 0, cache_read: 0 };
        const row = out[model][where];
        for (const [field, key] of [['uncached_input_tokens', 'uncached'],
                                    ['cache_read_input_tokens', 'cache_read']]) {
          const n = Number(result[field] ?? 0);
          if (Number.isFinite(n)) row[key] += Math.trunc(n);
        }
      }
    }
  }
  return out;
}

/**
 * Share of BANDED uncached input sitting in the 200k-1M band. Pure.
 * Banded only: unbanded traffic cannot be placed on either side, and putting it
 * in the denominator makes a workload look shorter than it is.
 */
export function longShare(modelRows) {
  const rows = modelRows ?? {};
  const short = Number(rows[SHORT_BAND]?.uncached ?? 0) || 0;
  const long = Number(rows[LONG_BAND]?.uncached ?? 0) || 0;
  const banded = short + long;
  if (banded <= 0) return 0;
  return long / banded;
}

/**
 * Share of a band's input read back from cache. Pure. Grades severity, not
 * diagnosis: a cached long prefix costs a tenth and is exactly as long.
 */
export function cachedShare(row) {
  const reads = Number(row?.cache_read ?? 0) || 0;
  const uncached = Number(row?.uncached ?? 0) || 0;
  const total = reads + uncached;
  if (total <= 0) return 0;
  return reads / total;
}

/**
 * Dollars for a number of uncached input tokens. Pure. The rate is passed in
 * rather than baked into a table: a price table in an audit script is a fact
 * with an expiry date and nothing warns you the day it passes.
 */
export function uncachedCost(tokens, ratePerMtok) {
  if (ratePerMtok < 0) throw new Error('ratePerMtok must not be negative');
  return Math.max(0, Math.trunc(Number(tokens ?? 0))) / 1e6 * Number(ratePerMtok);
}

/** Classify one model's context profile. Pure. Returns [state, detail]. */
export function verdict(modelRows, minTokens = 10000000, longThreshold = 0.25,
                        cacheFloor = 0.30) {
  const rows = modelRows ?? {};
  const banded = [SHORT_BAND, LONG_BAND]
    .reduce((a, b) => a + (Number(rows[b]?.uncached ?? 0) || 0), 0);
  const unbanded = Number(rows[UNBANDED]?.uncached ?? 0) || 0;
  const total = banded + unbanded;

  if (total < minTokens) {
    return ['low-volume',
      `${total} uncached input token(s) in the window, too few to conclude anything`];
  }
  if (banded <= 0) {
    return ['unbanded-only',
      `${(unbanded / 1e6).toFixed(1)}M uncached input token(s) with no ` +
      'context_window on any result, so this traffic cannot be placed in a band at all'];
  }

  const share = longShare(rows);
  const cached = cachedShare(rows[LONG_BAND]);
  const shape = `${(share * 100).toFixed(0)}% of banded uncached input is ` +
                `${LONG_BAND}, with ${(cached * 100).toFixed(0)}% of that band ` +
                'read from cache';

  if (share < longThreshold) {
    return ['short-context',
      `${shape}. The prefix is not where the money is going here.`];
  }
  if (cached >= cacheFloor) {
    return ['long-context-cached',
      `${shape}. The big prefix is being read back rather than reprocessed, so ` +
      'it costs a tenth of full rate. It is still just as long, and length is ' +
      'what degrades the answer.'];
  }
  return ['long-context-uncached',
    `${shape}. A very large prefix reprocessed from scratch on every call. ` +
    'Standard rates, extraordinary volume.'];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function readPages(key, path, params) {
  const out = [];
  let next = { ...params };
  for (;;) {
    const page = await get(key, path, next);
    out.push(page);
    if (!page.has_more || !page.next_page) return out;
    next = { ...next, page: page.next_page };
  }
}

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. */
function windowStart(days) {
  const midnight = new Date();
  midnight.setUTCHours(0, 0, 0, 0);
  midnight.setUTCDate(midnight.getUTCDate() - days);
  return midnight.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function main() {
  const key = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!key) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const inputRate = Number((process.env.INPUT_RATE || "dummy-input-rate") ?? 5.0);
  const minTokens = Number((process.env.MIN_TOKENS || "dummy-min-tokens") ?? 10000000);

  const rows = fold(await readPages(key, '/organizations/usage_report/messages', {
    starting_at: windowStart(days), bucket_width: '1d',
    limit: Math.min(days + 1, 31),
    'group_by[]': ['context_window', 'model'],
  }));

  let checked = 0;
  let bad = 0;
  const models = Object.keys(rows).sort(
    (a, b) => (rows[b][LONG_BAND]?.uncached ?? 0) - (rows[a][LONG_BAND]?.uncached ?? 0));
  for (const model of models) {
    const [state, detail] = verdict(rows[model], minTokens);
    checked += 1;
    const line = `${state.padEnd(22)} ${model.padEnd(22)} ${detail}`;

    if (state === 'long-context-cached') {
      console.warn(line);
      console.warn('  note: caching fixed the price and not the length. ' +
                   'Compaction is still the lever for answer quality.');
      continue;
    }
    if (!FINDINGS.includes(state)) {
      console.log(line);
      continue;
    }

    bad += 1;
    console.warn(line);
    const tokens = rows[model][LONG_BAND]?.uncached ?? 0;
    console.warn(`  ${(tokens / 1e6).toFixed(1)}M uncached token(s) in the band, ` +
                 `about $${uncachedCost(tokens, inputRate).toFixed(2)} at ` +
                 `$${inputRate.toFixed(2)} per million`);
    console.warn('  repair: compact or edit the context on the routes generating ' +
                 '200k+ prefixes, then put a cache_control breakpoint on whatever ' +
                 'stays stable. In that order.');
    console.warn('  note: this band is not a premium price tier. It is standard ' +
                 'rates on a very large number of tokens.');
  }

  const unbanded = Object.values(rows)
    .reduce((a, r) => a + (r[UNBANDED]?.uncached ?? 0), 0);
  if (unbanded) {
    console.log(`${(unbanded / 1e6).toFixed(1)}M uncached token(s) carried no ` +
                'context_window and were excluded from every share above');
  }

  console.log(`${checked} model(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
