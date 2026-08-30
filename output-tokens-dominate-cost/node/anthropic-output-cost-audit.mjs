/**
 * Report which side of a Claude request the bill is actually on.
 *
 * Read only. Two GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an
 * Admin API key (sk-ant-admin...), because every /v1/organizations endpoint
 * rejects a workspace key. The repair is printed, never performed.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// Output is priced at five times input on every current model.
export const OUTPUT_MULTIPLE = 5;

/**
 * Read a cost row's amount as a number. Pure. The cost report returns amount as
 * a decimal STRING; adding the raw values concatenates them instead of summing.
 */
export function amount(row) {
  const raw = row.amount;
  if (raw === null || raw === undefined || raw === '') return 0;
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Fold a token_type into one of five buckets. Pure. Matched on the shape of the
 * name, because new token types arrive with new cache durations and tiers;
 * anything unrecognised stays visible in "other" rather than being dropped.
 */
export function bucketOf(tokenType) {
  const name = String(tokenType ?? '').toLowerCase();
  if (!name) return 'other';
  if (name.includes('cache_creation') || name.includes('cache_write')) return 'cache_write';
  if (name.includes('cache_read')) return 'cache_read';
  if (name.includes('output')) return 'output';
  if (name.includes('input')) return 'input';
  return 'other';
}

/** Sum spend per token bucket across the cost report. Pure. */
export function byBucket(costBuckets) {
  const out = { input: 0, output: 0, cache_read: 0, cache_write: 0, other: 0 };
  for (const b of costBuckets) {
    for (const r of b.results ?? []) out[bucketOf(r.token_type)] += amount(r);
  }
  return out;
}

/**
 * The model carrying the most output tokens, and its share. Pure.
 * Returns [model, share] or [null, 0].
 */
export function topModel(usageBuckets) {
  const perModel = new Map();
  let total = 0;
  for (const b of usageBuckets) {
    for (const r of b.results ?? []) {
      const model = r.model ?? 'unspecified';
      const out = Number(r.output_tokens ?? 0);
      perModel.set(model, (perModel.get(model) ?? 0) + out);
      total += out;
    }
  }
  if (!total) return [null, 0];
  let best = null;
  for (const [m, v] of perModel) if (best === null || v > perModel.get(best)) best = m;
  return [best, perModel.get(best) / total];
}

/**
 * Turn the spend split into the lever that will actually move it. Pure. Each
 * state names a different repair; applying the wrong one changes nothing.
 * Returns [state, detail].
 */
export function verdict(buckets, minSpend = 1) {
  const total = Object.values(buckets).reduce((a, b) => a + b, 0);
  if (total < minSpend) {
    return ['no-spend', `$${total.toFixed(2)} over the window: nothing to act on`];
  }

  const pct = (k) => (buckets[k] / total) * 100;
  let split = `output ${pct('output').toFixed(0)}%, input ${pct('input').toFixed(0)}%, ` +
    `cache read ${pct('cache_read').toFixed(0)}%, cache write ${pct('cache_write').toFixed(0)}%`;
  if (buckets.other > 0) split += `, unrecognised ${pct('other').toFixed(0)}%`;
  const money = `$${total.toFixed(2)} over the window: ${split}`;

  if (buckets.cache_write > buckets.cache_read && pct('cache_write') >= 15) {
    return ['cache-write-heavy',
      `${money}. You are paying the cache write premium without the reads to ` +
      'amortise it: the prefix is being rewritten more often than it is hit.'];
  }

  if (pct('output') >= 70) {
    return ['output-dominated',
      `${money}. Output is priced at ${OUTPUT_MULTIPLE}x input and there is no ` +
      'caching discount on it, so the only lever is generating fewer tokens: ' +
      'lower effort, tighter stop conditions, shorter output formats.'];
  }

  if (pct('input') + pct('cache_read') + pct('cache_write') >= 60) {
    return ['input-dominated',
      `${money}. This is the shape prompt caching is for. Cache the stable ` +
      'prefix and read it back; trimming output here buys very little.'];
  }

  if (pct('output') >= 50) {
    return ['output-led',
      `${money}. Output is the larger half but not overwhelmingly. Both levers ` +
      'help and neither is dramatic on its own.'];
  }

  return ['balanced', money];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of params) url.searchParams.append(k, v);
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations endpoints ` +
                    'need an Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function readAll(key, path, params) {
  const out = [];
  let p = params;
  for (;;) {
    const page = await get(key, path, p);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) break;
    p = p.filter((x) => x[0] !== 'page').concat([['page', page.next_page]]);
  }
  return out;
}

async function main() {
  const key = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!key) {
    console.error('set ANTHROPIC_ADMIN_KEY (an Admin API key, sk-ant-admin...; ' +
                  'workspace keys are rejected by /v1/organizations/*)');
    process.exitCode = 2;
    return;
  }

  const argv = process.argv;
  const days = Number(argv.includes('--days') ? argv[argv.indexOf('--days') + 1] : 30) || 30;
  const since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10) +
    'T00:00:00Z';

  const costs = await readAll(key, '/organizations/cost_report',
    [['starting_at', since], ['limit', '31'], ['group_by[]', 'description']]);
  const usage = await readAll(key, '/organizations/usage_report/messages',
    [['starting_at', since], ['bucket_width', '1d'], ['limit', '31'],
      ['group_by[]', 'model']]);

  const split = byBucket(costs);
  const [state, detail] = verdict(split);
  const line = `${state.padEnd(18)} ${detail}`;

  let bad = 0;
  if (['no-spend', 'balanced', 'input-dominated'].includes(state)) console.log(line);
  else { bad = 1; console.warn(line); }

  const [model, share] = topModel(usage);
  if (model) {
    console.log(`top model by output tokens: ${model} ` +
                `(${(share * 100).toFixed(0)}% of output)`);
    if (bad) {
      console.warn(`  repair, to run yourself: lower output_config.effort on ${model} ` +
                   '(high to medium is the usual first step), then re-read this same ' +
                   'daily series a week later. Thinking tokens bill as output, so ' +
                   'effort is the setting that moves this share.');
      console.warn('  never change an effort setting from inside an audit; the Admin ' +
                   'API cannot do it and neither should this.');
    }
  } else {
    console.log('no output tokens in the usage report for this window');
  }

  console.log(`${costs.length} cost bucket(s), ${usage.length} usage bucket(s) ` +
              `over ${days} day(s), ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
