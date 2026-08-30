/**
 * Turn OpenAI shutdown dates into a migration schedule, ordered by urgency.
 *
 * Read only. GET requests and nothing else: the models list needs a project key
 * set to Read Only, and the optional traffic join needs an organization admin
 * key. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400000;
const FLAGGED = ['urgent', 'due', 'expired', 'unreadable-date'];

/** Read a shutdown_date into a UTC date, or null when it cannot be read. */
export function parseDay(value) {
  const raw = String(value ?? '').trim().split('T')[0];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null;
  const ms = Date.parse(`${raw}T00:00:00Z`);
  return Number.isNaN(ms) ? null : new Date(ms);
}

/**
 * How the traffic column is described, including when there is none. null means
 * the admin key was not supplied, which is different from zero and has to read
 * differently, or an unmeasured id looks like an unused one.
 */
export function trafficNote(requests30d) {
  if (requests30d === null || requests30d === undefined) {
    return 'traffic unknown: no admin key, so this is ordered by date alone';
  }
  if (requests30d === 0) {
    return 'no requests in the last 30 days, so this is probably a string in a ' +
           'config file or a monthly job rather than live traffic';
  }
  return `${requests30d} request(s) in the last 30 days`;
}

/**
 * Classify one models-list entry into a place in the migration schedule. Pure,
 * and both thresholds are arguments: the right window is however long a model
 * change takes to evaluate and roll out where you work. Returns [state, detail].
 */
export function plan(model, today, windowDays = 90, urgentWithin = 30,
                     requests30d = null) {
  const raw = model.shutdown_date;
  if (raw === null || raw === undefined || String(raw).trim() === '') {
    return ['unscheduled',
      'no shutdown date published today. Re-read the field rather than ' +
      'trusting this answer for a quarter.'];
  }

  const day = parseDay(raw);
  if (day === null) {
    return ['unreadable-date',
      `shutdown_date is ${JSON.stringify(raw)}, which this script will not guess at.`];
  }

  const iso = day.toISOString().slice(0, 10);
  const days = Math.round((day.getTime() - today.getTime()) / DAY);
  const note = trafficNote(requests30d);
  if (days < 0) {
    return ['expired',
      `shut down ${-days} day(s) ago on ${iso}. This is past planning; calls ` +
      `naming it are already failing. ${note}`];
  }
  if (days <= urgentWithin) {
    return ['urgent',
      `${days} day(s) left, shutting down ${iso}. Under ${urgentWithin} days is ` +
      `scheduling work now, not next cycle. ${note}`];
  }
  if (days <= windowDays) {
    return ['due',
      `${days} day(s) left, shutting down ${iso}. Inside the ${windowDays} day ` +
      `window. ${note}`];
  }
  return ['later',
    `${days} day(s) left, shutting down ${iso}. Outside the window; nothing to ` +
    `do yet. ${note}`];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI on ${path}: check the key, and ` +
      'that an organization admin key is used for /organization/*');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

export async function usageByModel(adminKey, days) {
  if (!adminKey) return new Map();
  const start = Math.floor((Date.now() - days * DAY) / 1000);
  const totals = new Map();
  const params = { start_time: start, bucket_width: '1d',
                   'group_by[]': 'model', limit: 31 };
  for (;;) {
    const page = await get(adminKey, '/organization/usage/completions', params);
    for (const bucket of page.data ?? []) {
      for (const row of bucket.results ?? []) {
        if (!row.model) continue;
        totals.set(row.model,
          (totals.get(row.model) ?? 0) + Number(row.num_model_requests ?? 0));
      }
    }
    if (!page.has_more || !page.next_page) break;
    params.page = page.next_page;
  }
  return totals;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.warn('OPENAI_ADMIN_KEY is not set: the report will be ordered by ' +
                 'date alone, with no idea which ids carry traffic');
  }

  const arg = (name, fallback) => Number(process.argv.includes(name)
    ? process.argv[process.argv.indexOf(name) + 1] : fallback) || fallback;
  const windowDays = arg('--window', 90);
  const urgentWithin = arg('--urgent-within', 30);
  const usageDays = arg('--usage-days', 30);

  const { data = [] } = await get(key, '/models');
  const dated = data.filter((m) => String(m.shutdown_date ?? '').trim() !== '');
  const totals = await usageByModel(admin, usageDays);

  const today = new Date(`${new Date().toISOString().slice(0, 10)}T00:00:00Z`);
  const rows = dated.map((model) => {
    const modelId = String(model.id ?? '?');
    const seen = admin ? (totals.get(modelId) ?? 0) : null;
    const [state, detail] = plan(model, today, windowDays, urgentWithin, seen);
    const day = parseDay(model.shutdown_date);
    return { sort: day ? day.getTime() : Infinity, seen: seen ?? 0,
             state, modelId, detail };
  }).sort((a, b) => a.sort - b.sort || b.seen - a.seen);

  let flagged = 0;
  for (const row of rows) {
    const line = `${row.state.padEnd(14)} ${row.modelId}  ${row.detail}`;
    if (FLAGGED.includes(row.state)) {
      flagged += 1;
      console.warn(line);
      console.warn('  repair: pin the successor from the deprecations page, then ' +
        're-run this against the new id so its own date is on the calendar ' +
        'before it is a surprise');
    } else {
      console.log(line);
    }
  }

  console.log(`${dated.length} dated model(s), ${flagged} inside a ${windowDays} day window`);
  process.exitCode = flagged ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
