/**
 * Report the per-search tool fee Claude web search is adding to the bill.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path, and an Admin key can be provisioned read-only.
 *
 * The fee is not a token price. Web search bills $10 per 1,000 searches on top
 * of the tokens, so no graph built on input and output tokens can show it. The
 * repair is printed, never applied.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// $10 per 1,000 searches, charged per search regardless of how many results
// come back. The unit is in the name because the natural slip is to multiply by
// ten and quote a bill a thousand times too large.
const FEE_PER_THOUSAND = 10.0;

// The cost report's own name for the row. Server tool money does not arrive as
// a token_type; it arrives under its own cost_type.
const COST_TYPE = 'web_search';

const FINDINGS = ['search-fee'];

/**
 * Sum server tool invocations per API key. Pure.
 *
 * server_tool_use is a nested object sitting beside the token fields, not one
 * of them. Walking the result flat finds nothing. Counters other than
 * web_search_requests are kept under their own names, because new server tools
 * ship and a script that sums one field keeps printing a reassuring number.
 */
export function fold(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        const key = String(result.api_key_id ?? 'unattributed');
        if (!out[key]) out[key] = { web_search: 0, other_tools: {} };
        const row = out[key];
        const use = result.server_tool_use;
        if (use === null || typeof use !== 'object' || Array.isArray(use)) continue;
        for (const [name, value] of Object.entries(use)) {
          const count = Math.trunc(Number(value ?? 0));
          if (!Number.isFinite(count) || count <= 0) continue;
          if (name === 'web_search_requests') row.web_search += count;
          else row.other_tools[name] = (row.other_tools[name] ?? 0) + count;
        }
      }
    }
  }
  return out;
}

/** Dollars owed for a number of searches. Pure. */
export function fee(searches, perThousand = FEE_PER_THOUSAND) {
  const n = Math.trunc(Number(searches ?? 0));
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, n) * perThousand / 1000;
}

/**
 * Sum the cost report rows the platform itself calls web search. Pure.
 * amount arrives as a decimal string, not a number.
 */
export function searchSpend(costBuckets, costType = COST_TYPE) {
  let total = 0;
  for (const bucket of costBuckets ?? []) {
    for (const result of bucket.results ?? []) {
      if (String(result.cost_type ?? '') !== costType) continue;
      const raw = result.amount;
      if (raw === null || raw === undefined || raw === '') continue;
      const value = Number(raw);
      if (Number.isFinite(value)) total += value;
    }
  }
  return total;
}

/**
 * Classify one key's search volume. Pure. Returns [state, detail].
 * The floor exists because a handful of searches is a demo, not a bill.
 */
export function verdict(row, minSearches = 100) {
  const searches = Math.trunc(Number(row?.web_search ?? 0)) || 0;
  if (searches <= 0) {
    return ['no-searches', 'the web search tool was never invoked by this key'];
  }
  if (searches < minSearches) {
    return ['low-volume',
      `${searches} search(es), under the floor of ${minSearches}, worth about ` +
      `$${fee(searches).toFixed(2)}`];
  }
  return ['search-fee',
    `${searches} search(es) at $${FEE_PER_THOUSAND.toFixed(0)} per 1,000, a ` +
    `tool fee of about $${fee(searches).toFixed(2)} before a single token is priced`];
}

/**
 * Compare the estimate against what was actually charged. Pure.
 *
 * Four states, not two. An errored search is counted as a use and not billed,
 * so the estimate may legitimately run ahead, and the cost report also lags.
 * Neither is a licence to present one number as the other.
 */
export function reconcile(estimate, billed, tolerance = 0.25) {
  if (estimate <= 0 && billed <= 0) {
    return ['no-searches', 'no searches counted and no web_search row billed'];
  }
  if (billed <= 0) {
    return ['unpriced',
      `$${estimate.toFixed(2)} of searches counted and no web_search row on ` +
      'the cost report. Either the report has not caught up with the window, ' +
      'or the searches errored and were never billed.'];
  }
  if (estimate <= 0) {
    return ['billed-without-count',
      `$${billed.toFixed(2)} billed as web_search with no searches counted. ` +
      'The two reports are not covering the same days.'];
  }
  const drift = Math.abs(billed - estimate) / estimate;
  if (drift <= tolerance) {
    return ['confirmed',
      `$${billed.toFixed(2)} billed against $${estimate.toFixed(2)} estimated, ` +
      `within ${(tolerance * 100).toFixed(0)}%`];
  }
  return ['mismatch',
    `$${billed.toFixed(2)} billed against $${estimate.toFixed(2)} estimated, ` +
    `${(drift * 100).toFixed(0)}% apart. Read the web_search rows directly ` +
    'before quoting either number.'];
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
  const minSearches = Number((process.env.MIN_SEARCHES || "dummy-min-searches") ?? 100);
  const showAll = process.argv.includes('--show-all');
  const start = windowStart(days);

  const usage = await readPages(key, '/organizations/usage_report/messages', {
    starting_at: start, bucket_width: '1d', limit: Math.min(days + 1, 31),
    'group_by[]': ['api_key_id'],
  });
  const rows = fold(usage);

  const costBuckets = [];
  for (const page of await readPages(key, '/organizations/cost_report',
    { starting_at: start, limit: 31, 'group_by[]': ['description'] })) {
    costBuckets.push(...(page.data ?? []));
  }

  let checked = 0;
  let bad = 0;
  let estimate = 0;
  const keys = Object.keys(rows).sort((a, b) => rows[b].web_search - rows[a].web_search);
  for (const id of keys) {
    const row = rows[id];
    const [state, detail] = verdict(row, minSearches);
    checked += 1;
    estimate += fee(row.web_search);
    const line = `${state.padEnd(14)} ${id.padEnd(14)} ${detail}`;

    if (FINDINGS.includes(state)) {
      bad += 1;
      console.warn(line);
      console.warn('  repair: set max_uses on the web search tool definition ' +
                   'for this service and narrow allowed_domains to the hosts ' +
                   'its answers actually cite');
      console.warn('  note: search results also re-enter input tokens on every ' +
                   'later turn of the same conversation, which is a second ' +
                   'charge this fee does not include');
    } else if (state === 'low-volume' || showAll) {
      console.log(line);
    }

    for (const name of Object.keys(row.other_tools).sort()) {
      console.log(`  other server tool ${name}: ${row.other_tools[name]} use(s) by ${id}`);
    }
  }

  const billed = searchSpend(costBuckets);
  const [state, detail] = reconcile(estimate, billed);
  const say = (state === 'confirmed' || state === 'no-searches') ? console.log : console.warn;
  say(`${state.padEnd(14)} ${detail}`);

  console.log(`${checked} key(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main(), fail on the missing key, and set an exit code that
// fails the suite even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
