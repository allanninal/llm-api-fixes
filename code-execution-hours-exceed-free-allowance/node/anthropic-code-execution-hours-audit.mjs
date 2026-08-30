/**
 * Report that Claude code execution has spent its free container hours.
 *
 * Read only. GET requests and nothing else against the Admin API, which needs
 * an Admin API key (sk-ant-admin...); a workspace key is rejected by every
 * /v1/organizations/* path.
 *
 * The finding has no threshold. 1,550 free container hours per organization per
 * month are consumed before anything is billed, so a non-zero amount on a
 * code_execution cost row means the allowance is already gone. The messages
 * usage report does not carry this line under any grouping.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// Free container hours per organization per month, consumed before anything is
// charged. That is what makes any non-zero amount a finding on its own.
const FREE_CONTAINER_HOURS = 1550;

// Published price per container hour, and the per-execution minimum. Prices
// rather than fields, so they are constants rather than something this script
// pretends to have read from the API.
const HOURLY_RATE = 0.05;
const MINIMUM_MINUTES = 5;

const COST_TYPE = 'code_execution';

const FINDINGS = ['allowance-just-crossed', 'allowance-spent', 'allowance-dwarfed'];

/** Read a cost row's amount as a number. Pure. amount is a decimal STRING. */
export function amount(row) {
  const raw = row?.amount;
  if (raw === null || raw === undefined || raw === '') return 0;
  const value = Number(raw);
  return Number.isFinite(value) ? value : 0;
}

/**
 * Sum spend into {workspace_id: {cost_type: dollars}}. Pure.
 * Every cost_type is kept. A filter that discards what it does not recognise is
 * how the next billable surface stays invisible for a quarter.
 */
export function fold(costBuckets) {
  const out = {};
  for (const bucket of costBuckets ?? []) {
    for (const result of bucket.results ?? []) {
      const workspace = String(result.workspace_id ?? 'default workspace');
      const kind = String(result.cost_type ?? 'unspecified');
      if (!out[workspace]) out[workspace] = {};
      out[workspace][kind] = (out[workspace][kind] ?? 0) + amount(result);
    }
  }
  return out;
}

/** Dollars of code execution per workspace, zeros dropped. Pure. */
export function codeExecutionSpend(folded, costType = COST_TYPE) {
  const out = {};
  for (const [workspace, types] of Object.entries(folded ?? {})) {
    if ((types[costType] ?? 0) > 0) out[workspace] = types[costType];
  }
  return out;
}

/**
 * Container hours behind a dollar amount. Pure.
 *
 * Rounded rather than left raw. Dollars are a decimal quantity and 0.05 has no
 * exact binary representation, so 84.60 / 0.05 comes out at 1691.9999999999998
 * and every later truncation reports an hour that was never missing.
 */
export function billedHours(dollars, rate = HOURLY_RATE) {
  if (rate <= 0) throw new Error('rate must be positive');
  return Math.round(Math.max(0, Number(dollars ?? 0)) / rate * 1e6) / 1e6;
}

/**
 * The most executions that could account for these hours. Pure.
 * A ceiling and never a count: every execution bills at least the minimum, and
 * the API reports no execution count at all.
 */
export function executionsCeiling(hours, minimumMinutes = MINIMUM_MINUTES) {
  if (minimumMinutes <= 0) throw new Error('minimumMinutes must be positive');
  return Math.trunc(Math.max(0, Number(hours ?? 0)) * 60 / minimumMinutes);
}

/**
 * Does the messages usage report carry this line anywhere? Pure.
 * The answer today is no, under any grouping. The check exists so the script
 * states that as an observation, and so it notices the day one appears.
 */
export function usageReportMentionsCodeExecution(pages) {
  for (const page of pages ?? []) {
    for (const bucket of page.data ?? []) {
      for (const result of bucket.results ?? []) {
        for (const name of Object.keys(result ?? {})) {
          if (String(name).toLowerCase().includes('code_execution')) return true;
        }
      }
    }
  }
  return false;
}

/**
 * Classify one workspace's code execution spend. Pure. Returns [state, detail].
 * No threshold to tune: the platform consumed the free allowance before it
 * wrote the row, so zero is inside it and anything else is past it.
 */
export function verdict(dollars, freeHours = FREE_CONTAINER_HOURS,
                        rate = HOURLY_RATE, marginal = 5.0) {
  const spend = Number(dollars ?? 0);
  if (!(spend > 0)) {
    return ['within-allowance',
      `no code_execution rows, so the free ${freeHours} container hour(s) cover ` +
      'this workspace, or the tool is bundled free with a current web search ' +
      'or web fetch version'];
  }

  const hours = billedHours(spend, rate);
  const shape = `$${spend.toFixed(2)} billed, which is ${Math.trunc(hours)} ` +
                `container hour(s) on top of the free ${freeHours}`;

  if (spend < marginal) {
    return ['allowance-just-crossed',
      `${shape}. The allowance is gone; the overage is still small enough to ` +
      'fix before it is not.'];
  }
  if (hours > freeHours) {
    return ['allowance-dwarfed',
      `${shape}. Billed hours now exceed the whole free allowance, so the free ` +
      'tier has stopped being a meaningful part of this bill.'];
  }
  return ['allowance-spent',
    `${shape}. Container time is being charged on every execution from here to ` +
    'the end of the month.'];
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

/** First of the current month, midnight UTC. The allowance resets monthly. */
function monthStart() {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))
    .toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** Midnight UTC, days ago, for a deliberate rolling read. */
function rollingStart(days) {
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

  const days = Number((process.env.DAYS || "dummy-days") ?? 0);
  const rate = Number((process.env.RATE || "dummy-rate") ?? HOURLY_RATE);
  const start = days ? rollingStart(days) : monthStart();
  if (days) {
    console.warn(`reading a rolling ${days} day window: the free allowance ` +
                 'resets monthly, so this may span two of them');
  }

  const costBuckets = [];
  for (const page of await readPages(key, '/organizations/cost_report',
    { starting_at: start, limit: 31, 'group_by[]': ['description', 'workspace_id'] })) {
    costBuckets.push(...(page.data ?? []));
  }

  const folded = fold(costBuckets);
  const spend = codeExecutionSpend(folded);

  let bad = 0;
  const workspaces = Object.keys(folded).sort(
    (a, b) => (folded[b][COST_TYPE] ?? 0) - (folded[a][COST_TYPE] ?? 0));
  for (const workspace of workspaces) {
    const [state, detail] = verdict(spend[workspace] ?? 0,
                                    FREE_CONTAINER_HOURS, rate);
    const line = `${state.padEnd(24)} ${workspace.padEnd(16)} ${detail}`;
    if (!FINDINGS.includes(state)) {
      console.log(line);
      continue;
    }
    bad += 1;
    console.warn(line);
    const hours = billedHours(spend[workspace], rate);
    console.warn(`  at the ${MINIMUM_MINUTES} minute minimum that is at most ` +
                 `${executionsCeiling(hours)} execution(s)`);
    console.warn('  repair: find the routes attaching files to requests that ' +
                 'never call the tool. Attached files are preloaded onto a ' +
                 'container and bill time whether the tool runs or not.');
    console.warn('  repair: bundling code execution with web_search_20260209 ' +
                 'or web_fetch_20260209 or later removes the charge entirely');
  }

  const seen = [...new Set(Object.values(folded).flatMap((t) => Object.keys(t)))].sort();
  console.log(`cost_type values in this window: ${seen.join(', ') || 'none'}`);

  const usage = await readPages(key, '/organizations/usage_report/messages',
    { starting_at: start, bucket_width: '1d', limit: 1 });
  if (usageReportMentionsCodeExecution(usage.slice(0, 1))) {
    console.warn('the messages usage report now carries a code execution field: ' +
                 'read it, this script predates it');
  } else {
    console.log('note: the messages usage report carries no code execution ' +
                'field at all, which is why this check reads the cost report');
  }

  console.log(`${Object.keys(folded).length} workspace(s) with cost, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
