/**
 * Report synchronous OpenAI traffic that is shaped like batch work.
 *
 * Read only. Two GET requests against the organization endpoints and nothing
 * else. Those endpoints reject project keys, so this needs an organization
 * admin key (sk-admin-), which can and should be provisioned read-only.
 *
 * This is a cost note, not a failure note. Nothing found here is broken.
 */
const API = 'https://api.openai.com/v1';

// The Batch API is priced at half the synchronous rate on both input and output
// tokens, in exchange for a completion window of up to 24 hours.
const DISCOUNT = 0.50;

/**
 * Fold usage buckets into one row per project and model. Pure. The hourly
 * request counts stay aligned across the whole window, with zeros where a
 * workload was idle: compacting the idle hours out would make every workload
 * look concentrated, which is the thing being measured.
 */
export function accumulate(buckets) {
  const list = buckets ?? [];
  const rows = new Map();
  list.forEach((bucket, index) => {
    for (const result of bucket.results ?? []) {
      const project = String(result.project_id ?? 'unknown');
      const model = String(result.model ?? 'unknown');
      const key = `${project} / ${model}`;
      let row = rows.get(key);
      if (!row) {
        row = {
          key,
          project_id: project,
          model,
          sync_requests: 0,
          batch_requests: 0,
          sync_input: 0,
          sync_output: 0,
          hourly: new Array(list.length).fill(0),
        };
        rows.set(key, row);
      }
      const made = Number(result.num_model_requests ?? 0) || 0;
      if (result.batch === true) {
        row.batch_requests += made;
      } else {
        row.sync_requests += made;
        row.sync_input += Number(result.input_tokens ?? 0) || 0;
        row.sync_output += Number(result.output_tokens ?? 0) || 0;
        row.hourly[index] += made;
      }
    }
  });
  return rows;
}

/**
 * Share of requests inside the busiest slice of the window. Pure. Returns a
 * number between 0 and 1, or null when there is nothing to measure.
 */
export function concentration(hourly, topFraction = 0.10) {
  const counts = (hourly ?? []).map((c) => Number(c) || 0);
  const total = counts.reduce((a, b) => a + b, 0);
  if (counts.length === 0 || total <= 0) return null;
  const top = Math.max(1, Math.ceil(counts.length * topFraction));
  const busiest = [...counts].sort((a, b) => b - a).slice(0, top);
  return busiest.reduce((a, b) => a + b, 0) / total;
}

/**
 * Classify one workload's week. Pure. Returns [state, detail]. "interactive"
 * and "already-batched" are answers rather than failures to detect something:
 * synchronous is correct for traffic with a person waiting on it.
 */
export function verdict(row, minRequests = 1000, threshold = 0.70,
                        topFraction = 0.10) {
  const sync = Number(row.sync_requests ?? 0) || 0;
  const batched = Number(row.batch_requests ?? 0) || 0;
  const total = sync + batched;

  if (total < minRequests) {
    return ['too-little-traffic',
      `${total} request(s) in the window, which is too few to say anything ` +
      'about the shape'];
  }

  const share = sync / total;
  if (share < 0.20) {
    return ['already-batched',
      `${Math.round(100 * (1 - share))}% of ${total} request(s) already go ` +
      'through the Batch API'];
  }

  const spike = concentration(row.hourly, topFraction);
  if (spike === null) {
    return ['unmeasurable',
      `${sync} synchronous request(s) and no per bucket counts to spread them ` +
      'over, so the shape cannot be measured'];
  }

  const pct = Math.round(spike * 100);
  const slice = Math.round(topFraction * 100);
  if (spike >= threshold) {
    return ['batch-shaped',
      `${pct}% of ${sync} synchronous request(s) land in the busiest ` +
      `${slice}% of hours. That is a schedule, not an audience, and it is ` +
      'paying interactive prices.'];
  }
  return ['interactive',
    `${sync} synchronous request(s), ${pct}% of them in the busiest ${slice}% ` +
    'of hours. Spread out like traffic with someone waiting on it, so the ' +
    'synchronous endpoint is the right one.'];
}

/**
 * Non-batch dollars in the cost report, optionally for one project. Pure.
 * Batch and non-batch appear as distinct line_item strings, so the split is a
 * substring test and nothing more clever than that.
 */
export function syncCost(buckets, projectId = null) {
  let total = 0;
  for (const bucket of buckets ?? []) {
    for (const result of bucket.results ?? []) {
      if (projectId && String(result.project_id ?? '') !== projectId) continue;
      if (String(result.line_item ?? '').toLowerCase().includes('batch')) continue;
      total += Number(result.amount?.value ?? 0) || 0;
    }
  }
  return Math.round(total * 100) / 100;
}

/**
 * What the same spend would have been worth at batch prices. Pure. Not a
 * promise: it says nothing about whether the job can accept a 24 hour window.
 */
export function saving(syncCostUsd, discount = DISCOUNT) {
  if (syncCostUsd === null || syncCostUsd === undefined) return null;
  const value = Number(syncCostUsd);
  if (!Number.isFinite(value)) return null;
  return Math.round(Math.max(0, value) * discount * 100) / 100;
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.forEach((one) => url.searchParams.append(k, String(one)));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function pages(key, path, params, maxPages = 40) {
  const out = [];
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, path, query);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) break;
    query = { ...params, page: page.next_page };
  }
  return out;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key") ?? (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key, read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const minRequests = Number((process.env.MIN_REQUESTS || "dummy-min-requests") ?? 1000);
  const threshold = Number((process.env.THRESHOLD || "dummy-threshold") ?? 0.70);
  const topFraction = Number((process.env.TOP_FRACTION || "dummy-top-fraction") ?? 0.10);
  const showAll = process.argv.includes('--show-all');

  const start = Math.floor(Date.now() / 1000) - days * 86400;
  const usage = await pages(key, '/organization/usage/completions', {
    start_time: start,
    bucket_width: '1h',
    limit: 168,
    group_by: ['batch', 'project_id', 'model'],
  });
  const costs = await pages(key, '/organization/costs', {
    start_time: start,
    bucket_width: '1d',
    limit: 31,
    group_by: ['line_item', 'project_id'],
  });

  const rows = accumulate(usage);
  if (rows.size === 0) {
    console.log(`no completions usage in the last ${days} day(s) for this ` +
                'organization');
    return;
  }

  let found = 0;
  for (const name of [...rows.keys()].sort()) {
    const row = rows.get(name);
    const [state, detail] = verdict(row, minRequests, threshold, topFraction);
    const line = `${state.padEnd(17)} ${name}  ${detail}`;
    if (state === 'batch-shaped') {
      found += 1;
      console.warn(line);
      const spend = syncCost(costs, row.project_id);
      console.warn(`  cost: $${spend.toFixed(2)} of synchronous spend on ` +
        `project ${row.project_id} over ${days} day(s); about ` +
        `$${saving(spend).toFixed(2)} of that is the batch discount you are ` +
        'not taking');
      console.warn('  repair: upload the requests as a .jsonl to /v1/files ' +
        'with purpose=batch, create a batch with a 24h completion window, and ' +
        'read both result files. The trade is half price for no latency ' +
        'guarantee.');
    } else if (['interactive', 'already-batched', 'too-little-traffic'].includes(state)) {
      if (showAll) console.log(line);
    } else {
      console.warn(line);
    }
  }

  console.log(`${rows.size} workload(s), ${found} batch shaped`);
  process.exitCode = found ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
