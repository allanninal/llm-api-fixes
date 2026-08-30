/**
 * Report a change in organization spend and say what shape the change is.
 *
 * Read only. One paginated GET against whichever provider you point it at.
 * Both cost reports need an organization admin key: OpenAI's rejects project
 * keys, Anthropic's rejects workspace keys. Read-only admin keys work.
 */
const OPENAI_API = 'https://api.openai.com/v1';
const ANTHROPIC_API = 'https://api.anthropic.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

const DAY = 86400000;

const FINDINGS = ['spike', 'step', 'ramp', 'drop', 'new-spend'];

/** An ISO day string to a day count since the epoch, or null. */
function dayNumber(text) {
  const parsed = Date.parse(`${String(text ?? '').slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(parsed) ? null : Math.round(parsed / DAY);
}

function dayIso(number) {
  return new Date(number * DAY).toISOString().slice(0, 10);
}

/**
 * Anthropic's decimal string of cents to integer millicents. Pure. Returns
 * null on anything unparseable, which the caller skips rather than reading as
 * zero. Integers rather than floats because summing 56 buckets of float cents
 * is how a total ends up a cent adrift.
 */
export function parseCents(text) {
  let raw = String(text ?? '').trim();
  if (!raw) return null;
  const negative = raw.startsWith('-');
  if (raw[0] === '+' || raw[0] === '-') raw = raw.slice(1);
  const dot = raw.indexOf('.');
  const whole = (dot < 0 ? raw : raw.slice(0, dot)) || '0';
  const frac = `${dot < 0 ? '' : raw.slice(dot + 1)}000`.slice(0, 3);
  if (!/^\d+$/.test(whole) || !/^\d+$/.test(frac)) return null;
  const value = Number(whole) * 1000 + Number(frac);
  return negative ? -value : value;
}

/**
 * Fold GET /v1/organization/costs into {day: dollars}. Pure. amount.value is a
 * float in dollars and start_time is a Unix timestamp.
 */
export function dailyFromOpenai(buckets) {
  const days = new Map();
  for (const bucket of buckets ?? []) {
    const opened = Number(bucket.start_time);
    if (!Number.isFinite(opened)) continue;
    const key = new Date(opened * 1000).toISOString().slice(0, 10);
    for (const result of bucket.results ?? []) {
      const value = Number(result.amount?.value ?? 0);
      if (!Number.isFinite(value)) continue;
      days.set(key, Math.round(((days.get(key) ?? 0) + value) * 1e6) / 1e6);
    }
  }
  return days;
}

/**
 * Fold GET /v1/organizations/cost_report into {day: dollars}. Pure. amount is
 * a decimal string in cents, parsed exactly and converted once at the end.
 */
export function dailyFromAnthropic(buckets) {
  const days = new Map();
  for (const bucket of buckets ?? []) {
    const key = String(bucket.starting_at ?? '').slice(0, 10);
    if (dayNumber(key) === null) continue;
    for (const result of bucket.results ?? []) {
      const millicents = parseCents(result.amount);
      if (millicents === null) continue;
      days.set(key, (days.get(key) ?? 0) + millicents);
    }
  }
  const out = new Map();
  for (const [day, total] of days) out.set(day, Math.round(total / 10) / 10000);
  return out;
}

/**
 * Fold a day-to-dollars map into whole weeks, newest first. Pure. Returns
 * [[firstDay, lastDay, dollars], ...]. Today is excluded, always: the current
 * bucket is partial and a comparison that includes it reports a fall in spend
 * every time it runs before lunch.
 */
export function weeks(daily, today, count = 8) {
  const end = dayNumber(today);
  if (end === null) return [];
  const entries = daily instanceof Map ? [...daily] : Object.entries(daily ?? {});
  const totals = new Map();
  for (const [key, value] of entries) {
    const number = dayNumber(key);
    const amount = Number(value);
    if (number === null || number >= end || !Number.isFinite(amount)) continue;
    totals.set(number, (totals.get(number) ?? 0) + amount);
  }
  if (totals.size === 0) return [];

  const numbers = [...totals.keys()];
  const first = Math.min(...numbers);
  let stop = Math.min(end, Math.max(...numbers) + 1);
  const out = [];
  while (out.length < Number(count)) {
    const start = stop - 7;
    if (start < first) break;
    let total = 0;
    for (let day = start; day < stop; day += 1) total += totals.get(day) ?? 0;
    out.push([dayIso(start), dayIso(stop - 1), Math.round(total * 100) / 100]);
    stop = start;
  }
  return out;
}

/**
 * Classify a list of weekly totals, newest first. Pure. Returns [state,
 * detail]. Three ways for spend to be higher and three different people to
 * call: a spike is a job that ran once, a step is a level something shipped
 * into, a ramp is growth no week-over-week ratio will ever catch.
 */
export function classify(totals, threshold = 0.40, minWeeks = 3) {
  const series = [];
  for (const value of totals ?? []) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      return ['unreadable', 'a weekly total that is not a number'];
    }
    series.push(number);
  }

  if (series.length < Number(minWeeks)) {
    return ['too-short',
      `${series.length} whole week(s) of history, which is not enough to call ` +
      'anything a change'];
  }

  const latest = series[0];
  const prior = series.slice(1);
  const baseline = prior.reduce((a, b) => a + b, 0) / prior.length;
  if (baseline <= 0) {
    if (latest > 0) {
      return ['new-spend',
        `$${latest.toFixed(2)} in the latest week against nothing at all ` +
        'before it. This organization started spending inside the window.'];
    }
    return ['no-spend', `no spend in any of the ${series.length} week(s) read`];
  }

  const oldestFirst = [...series].reverse();
  const climbing = oldestFirst.every((v, i) => i === 0 || v > oldestFirst[i - 1]);
  if (series.length >= 4 && climbing && oldestFirst[0] > 0
      && (latest - oldestFirst[0]) / oldestFirst[0] > threshold) {
    const growth = 100 * (latest - oldestFirst[0]) / oldestFirst[0];
    return ['ramp',
      `every one of ${series.length} week(s) is higher than the one before it, ` +
      `$${oldestFirst[0].toFixed(2)} to $${latest.toFixed(2)} ` +
      `(+${growth.toFixed(0)}%). A week-over-week check never sees this, ` +
      'because the growth is already in the baseline.'];
  }

  const change = (latest - baseline) / baseline;
  if (change > threshold) {
    const older = series.slice(2);
    const olderBaseline = older.length
      ? older.reduce((a, b) => a + b, 0) / older.length : 0;
    if (olderBaseline > 0 && (series[1] - olderBaseline) / olderBaseline > threshold) {
      return ['step',
        `$${latest.toFixed(2)} in the latest week and $${series[1].toFixed(2)} ` +
        `in the one before it, against a $${olderBaseline.toFixed(2)} baseline ` +
        'before that. The new level has held for two weeks, so something ' +
        'shipped rather than ran once.'];
    }
    return ['spike',
      `$${latest.toFixed(2)} in the latest week against a ` +
      `$${baseline.toFixed(2)} baseline (+${(change * 100).toFixed(0)}%), and ` +
      'the week before it was normal. One week high is a job that ran, not a ' +
      'level that changed.'];
  }
  if (change < -threshold) {
    return ['drop',
      `$${latest.toFixed(2)} in the latest week against a ` +
      `$${baseline.toFixed(2)} baseline (${(change * 100).toFixed(0)}%). Spend ` +
      'falling this fast is usually traffic that stopped rather than money ' +
      'that was saved.'];
  }
  const signed = `${change >= 0 ? '+' : ''}${(change * 100).toFixed(1)}`;
  return ['flat',
    `$${latest.toFixed(2)} against a $${baseline.toFixed(2)} baseline (${signed}%)`];
}

async function get(url, params, headers) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) target.searchParams.set(k, String(v));
  }
  const res = await fetch(target, { headers });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from the cost report: this endpoint needs an ` +
                    'organization admin key, not a project or workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${target.pathname}`);
  return res.json();
}

async function readBuckets(url, params, headers, maxPages = 40) {
  const out = [];
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(url, query, headers);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) break;
    query = { ...params, page: page.next_page };
  }
  return out;
}

async function main() {
  const provider = (process.argv.find((a) => a.startsWith('--provider='))
    ?? '--provider=openai').slice('--provider='.length);
  const howMany = Number((process.env.WEEKS || "dummy-weeks") ?? 8);
  const threshold = Number((process.env.THRESHOLD || "dummy-threshold") ?? 0.40);
  const days = howMany * 7 + 1;

  let daily;
  if (provider === 'anthropic') {
    const key = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
    if (!key) {
      console.error('set ANTHROPIC_ADMIN_KEY (an Admin API key, sk-ant-admin)');
      process.exitCode = 2;
      return;
    }
    const startedAt = new Date(Date.now() - days * DAY).toISOString().slice(0, 10);
    const buckets = await readBuckets(`${ANTHROPIC_API}/organizations/cost_report`,
      { starting_at: `${startedAt}T00:00:00Z`, limit: 31 },
      { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION });
    daily = dailyFromAnthropic(buckets);
  } else {
    const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
    if (!key) {
      console.error('set OPENAI_ADMIN_KEY (an organization admin key, read-only ' +
                    'scopes are enough)');
      process.exitCode = 2;
      return;
    }
    const buckets = await readBuckets(`${OPENAI_API}/organization/costs`, {
      start_time: Math.floor((Date.now() - days * DAY) / 1000),
      bucket_width: '1d',
      limit: Math.min(180, Math.max(1, days)),
    }, { Authorization: `Bearer ${key}` });
    daily = dailyFromOpenai(buckets);
  }

  const today = new Date().toISOString().slice(0, 10);
  const series = weeks(daily, today, howMany);
  if (series.length === 0) {
    console.log('no whole weeks of cost data in the window');
    return;
  }

  const [state, detail] = classify(series.map(([, , total]) => total), threshold);
  const [first, last] = series[0];
  console.log(`${series.length} whole week(s) read, most recent ${first}..${last}`);
  for (const [weekFirst, weekLast, total] of series) {
    console.log(`  ${weekFirst}..${weekLast}  $${total.toFixed(2)}`);
  }

  if (FINDINGS.includes(state)) {
    console.warn(`${state.padEnd(11)} ${first}..${last}  ${detail}`);
    console.warn('  repair: attribute the delta before you act on it. Group the ' +
      'same window by line item and by project and read the rows that moved, ' +
      'rather than the rows you remember being expensive.');
    console.warn(provider === 'anthropic'
      ? '  repair: Anthropic has no spend-limit endpoint. Set the organization ' +
        'and per-workspace limits in the console, and re-read this window first ' +
        'because late events revise the recent past.'
      : '  repair: print, do not run. Set a ceiling with POST ' +
        "/v1/organization/spend_limit {'threshold_amount': <cents>, 'currency': " +
        "'USD', 'interval': 'month'} and an early warning with POST " +
        '/v1/organization/spend_alerts at about 60% of it.');
    process.exitCode = 1;
    return;
  }

  console.log(`${state.padEnd(11)} ${first}..${last}  ${detail}`);
  console.log(`${series.length} whole week(s) read, no change worth reporting`);
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
