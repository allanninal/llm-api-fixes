/**
 * Report whether anything would stop a runaway OpenAI bill.
 *
 * Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
 * organization admin key with read scopes, because every /v1/organization
 * endpoint rejects a project key. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

/**
 * Read threshold_amount as dollars, or null when no limit is configured. The
 * field is in CENTS; a value typed as dollars is 100x too low and takes
 * production down inside the hour, so the conversion lives in one named place.
 */
export function thresholdDollars(limit) {
  if (!limit || typeof limit !== 'object') return null;
  const obj = (limit.spend_limit && typeof limit.spend_limit === 'object')
    ? limit.spend_limit : limit;
  const raw = obj.threshold_amount;
  if (raw === null || raw === undefined || raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n / 100 : null;
}

/** Pro-rate month-to-date spend to a month-end figure. Pure, clock injected. */
export function projectedMonthEnd(spent, now) {
  const daysInMonth = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0))
    .getUTCDate();
  const elapsedHours = (now.getUTCDate() - 1) * 24 + now.getUTCHours()
    + now.getUTCMinutes() / 60;
  const totalHours = daysInMonth * 24;
  const fraction = Math.max(elapsedHours / totalHours, 1 / totalHours);
  return spent / fraction;
}

/** Alert recipients who are not members of the organization any more. Sorted. */
export function unknownRecipients(alerts, knownEmails) {
  const known = new Set(knownEmails.map((e) => String(e ?? '').trim().toLowerCase()));
  const missing = new Set();
  for (const a of alerts) {
    for (const r of a.notification_channel?.recipients ?? []) {
      if (!known.has(String(r).trim().toLowerCase())) missing.add(String(r));
    }
  }
  return [...missing].sort();
}

/**
 * Classify one scope's protection against a runaway. Pure. Returns
 * [state, detail].
 */
export function verdict(limit, alerts, spent, now) {
  const projected = projectedMonthEnd(spent, now);
  const threshold = thresholdDollars(limit);
  const money = `$${spent.toFixed(2)} month-to-date, projecting $${projected.toFixed(2)}`;

  if (threshold === null) {
    return ['no-limit',
      `${money}, and no spend limit is configured. Nothing in the platform will ` +
      'refuse a request no matter how much a runaway spends.'];
  }

  const obj = (limit.spend_limit && typeof limit.spend_limit === 'object')
    ? limit.spend_limit : limit;
  const status = String(obj.enforcement?.status ?? '');

  if (status && status !== 'enforcing') {
    return ['not-enforcing',
      `${money}. A limit of $${threshold.toFixed(2)} exists but ` +
      `enforcement.status is "${status}", so it displays and does not brake.`];
  }

  if (threshold * 100 <= projected) {
    return ['cents-mistake',
      `${money}, against a limit of $${threshold.toFixed(2)}. threshold_amount ` +
      'is in cents: a value this far below the run rate is almost always a ' +
      'figure typed as dollars, which is 100x too low and will page you ' +
      'immediately.'];
  }

  if (threshold <= spent) {
    return ['breached',
      `${money}, against a limit of $${threshold.toFixed(2)}. Requests are ` +
      'already being refused with 429 organization_spend_limit_exceeded.'];
  }

  if (threshold <= projected) {
    return ['will-breach',
      `${money}, against a limit of $${threshold.toFixed(2)}. At this run rate ` +
      'the brake engages before the interval resets.'];
  }

  if (threshold >= projected * 5) {
    return ['ceiling-too-high',
      `${money}, against a limit of $${threshold.toFixed(2)}. A ceiling more ` +
      'than five times the run rate cannot fire in time to be useful.'];
  }

  if (!alerts || alerts.length === 0) {
    return ['no-alerts',
      `${money}, with a limit of $${threshold.toFixed(2)} enforcing and no ` +
      'spend alerts. A brake with no warning light: the first signal is ' +
      'production returning 429.'];
  }

  return ['guarded',
    `${money}, limit $${threshold.toFixed(2)}, ${alerts.length} alert(s)`];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization endpoints need ` +
                    'an organization admin key, not a project key');
  }
  if (res.status === 404) return {};
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function monthToDate(key, now, projectId) {
  const start = Math.floor(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1) / 1000);
  const params = { start_time: start, limit: 31 };
  if (projectId) params.project_ids = projectId;
  const costs = await get(key, '/organization/costs', params);
  let total = 0;
  for (const b of costs.data ?? []) {
    for (const r of b.results ?? []) total += Number(r.amount?.value ?? 0);
  }
  return total;
}

function report(scope, limit, alerts, spent, now) {
  const [state, detail] = verdict(limit, alerts, spent, now);
  const line = `${state.padEnd(16)} ${String(scope).padEnd(24)} ${detail}`;
  if (state === 'guarded') { console.log(line); return 0; }
  console.warn(line);
  const suggested = Math.round(projectedMonthEnd(spent, now) * 2) * 100;
  console.warn(`  repair, to run yourself: POST ${API}/organization/spend_limit ` +
               `with a body of {"threshold_amount": ${suggested}, "currency": ` +
               `"USD", "interval": "month"} -- that is ${suggested} cents, which ` +
               `is $${(suggested / 100).toFixed(2)}.`);
  console.warn(`  then alerts at 50%, 75% and 90% of it via ` +
               `${API}/organization/spend_alerts, with a real recipients list.`);
  return 1;
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read ' +
                  'scopes; project keys are rejected by /v1/organization/*)');
    process.exitCode = 2;
    return;
  }

  const now = new Date();
  const limit = await get(admin, '/organization/spend_limit');
  const { data: alerts = [] } = await get(admin, '/organization/spend_alerts', { limit: 100 });
  const spent = await monthToDate(admin, now);

  let scopes = 1;
  let bad = report('organization', limit, alerts, spent, now);

  const { data: users = [] } = await get(admin, '/organization/users', { limit: 100 });
  const stale = unknownRecipients(alerts, users.map((u) => u.email));
  if (stale.length > 0) {
    bad += 1;
    console.warn(`${'stale-recipient'.padEnd(16)} ${'organization'.padEnd(24)} ` +
                 `alert recipients not in the organization: ${stale.join(', ')}`);
  }

  if (process.argv.includes('--projects')) {
    const { data: projects = [] } = await get(admin, '/organization/projects', { limit: 25 });
    for (const p of projects) {
      if (!p.id || (p.status ?? 'active') !== 'active') continue;
      scopes += 1;
      const plimit = await get(admin, `/organization/projects/${p.id}/spend_limit`);
      const { data: palerts = [] } = await get(
        admin, `/organization/projects/${p.id}/spend_alerts`, { limit: 100 });
      const pspent = await monthToDate(admin, now, p.id);
      bad += report(p.name ?? p.id, plimit, palerts, pspent, now);
    }
  }

  console.log(`${scopes} scope(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
