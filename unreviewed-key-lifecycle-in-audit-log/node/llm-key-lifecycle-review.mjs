/**
 * Read the key and member lifecycle events nobody has ever read.
 *
 * Read only. GET requests only, against the OpenAI Audit Logs API and the
 * Anthropic Compliance activity feed.
 *
 * Both feeds are pull-only, which is why the control exists everywhere and has
 * fired nowhere. The finding is not any single event; it is that nobody is
 * reading, so the last thing printed is a watermark for the next run.
 *
 * An empty feed is reported as unavailable and never as clean, and the
 * geography rule runs on OpenAI session actors only: the Anthropic activity
 * record has no country breakdown to test.
 */
const OPENAI = 'https://api.openai.com/v1';
const ANTHROPIC = 'https://api.anthropic.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

const OPENAI_EVENTS = ['api_key.created', 'api_key.updated', 'api_key.deleted',
                       'service_account.created', 'service_account.deleted',
                       'login.failed'];

export const OFF_ROSTER = 'off-roster-actor';
export const UNATTRIBUTABLE = 'unattributable';
export const UNEXPECTED_COUNTRY = 'unexpected-country';
export const OUT_OF_HOURS = 'out-of-hours';
export const REVIEWED = 'reviewed';

export const FEED_OK = 'feed-readable';
export const FEED_UNAVAILABLE = 'feed-unavailable';

const SEVERITY = [OFF_ROSTER, UNEXPECTED_COUNTRY, UNATTRIBUTABLE, OUT_OF_HOURS];

/** Epoch seconds from a unix integer or an RFC 3339 string. Pure. */
export function parseWhen(value) {
  if (value === null || value === undefined || value === '' ||
      typeof value === 'boolean') return null;
  if (typeof value === 'number') return Math.trunc(value);
  const text = String(value).trim();
  if (/^\d+$/.test(text)) return Number(text);
  const when = new Date(text);
  if (Number.isNaN(when.getTime())) return null;
  return Math.floor(when.getTime() / 1000);
}

/** A readable UTC timestamp. Pure. */
export function iso(epoch) {
  if (epoch === null || epoch === undefined) return '(no timestamp)';
  return `${new Date(Number(epoch) * 1000).toISOString().slice(0, 19)}Z`;
}

/** One audit-log entry in the common shape. Pure. Two actor shapes. */
export function normaliseOpenai(entry) {
  const row = entry ?? {};
  const actor = (row.actor && typeof row.actor === 'object') ? row.actor : {};
  const kind = String(actor.type ?? '').trim().toLowerCase();
  let email = null;
  let ip = null;
  let country = null;
  if (kind === 'session') {
    const session = (actor.session && typeof actor.session === 'object') ? actor.session : {};
    const user = (session.user && typeof session.user === 'object') ? session.user : {};
    email = user.email ?? null;
    ip = session.ip_address ?? null;
    const details = (session.ip_address_details &&
                     typeof session.ip_address_details === 'object')
      ? session.ip_address_details : {};
    country = details.country ?? null;
  } else if (kind === 'api_key') {
    const apiKey = (actor.api_key && typeof actor.api_key === 'object') ? actor.api_key : {};
    const user = (apiKey.user && typeof apiKey.user === 'object') ? apiKey.user : {};
    email = user.email ?? null;
  }
  const project = (row.project && typeof row.project === 'object') ? row.project : {};
  return { source: 'openai', type: String(row.type ?? '(untyped)'),
           when: parseWhen(row.effective_at), actorKind: kind || 'unknown',
           actorEmail: email ? String(email).trim().toLowerCase() : null,
           actorIp: ip, country,
           container: project.name ?? project.id ?? null };
}

/** One compliance activity in the common shape. Pure. country stays null. */
export function normaliseAnthropic(activity) {
  const row = activity ?? {};
  const actor = (row.actor && typeof row.actor === 'object') ? row.actor : {};
  const email = actor.email_address ?? null;
  return { source: 'anthropic', type: String(row.type ?? '(untyped)'),
           when: parseWhen(row.created_at),
           actorKind: email ? 'user' : 'unknown',
           actorEmail: email ? String(email).trim().toLowerCase() : null,
           actorIp: actor.ip_address ?? null, country: null,
           container: row.organization_id ?? null };
}

/** on-roster, off-roster or unattributable. Pure. */
export function resolveActor(event, roster) {
  const email = (event ?? {}).actorEmail;
  if (!email) return 'unattributable';
  return (roster ?? new Set()).has(String(email).trim().toLowerCase())
    ? 'on-roster' : 'off-roster';
}

/** The UTC hour of an event, or null. Pure. */
export function hourOf(event) {
  const when = (event ?? {}).when;
  if (when === null || when === undefined) return null;
  return new Date(Number(when) * 1000).getUTCHours();
}

/** Classify one normalised event. Pure. Returns [state, reasons]. */
export function grade(event, roster, businessHours = [7, 19], operatingCountries = null) {
  const reasons = [];
  const resolution = resolveActor(event, roster);
  if (resolution === 'off-roster') {
    reasons.push([OFF_ROSTER, 'the actor is not on the current roster']);
  } else if (resolution === 'unattributable') {
    reasons.push([UNATTRIBUTABLE,
      `an ${(event ?? {}).actorKind ?? 'unknown'} actor carries no user ` +
      'email, so no person can be attributed']);
  }

  const country = (event ?? {}).country;
  if (operatingCountries && operatingCountries.length && country) {
    const allowed = new Set(operatingCountries.map((c) => String(c).toUpperCase()));
    if (!allowed.has(String(country).trim().toUpperCase())) {
      reasons.push([UNEXPECTED_COUNTRY,
        `ip_address_details.country ${country} is outside the operating geographies`]);
    }
  }

  const hour = hourOf(event);
  const [start, end] = businessHours;
  const type = String((event ?? {}).type ?? '');
  const creation = type.endsWith('.created') || type.endsWith('.deleted');
  if (creation && hour !== null && !(hour >= start && hour < end)) {
    reasons.push([OUT_OF_HOURS,
      `created outside business hours (${String(hour).padStart(2, '0')}:00 UTC)`]);
  }

  if (!reasons.length) return [REVIEWED, []];
  const present = new Set(reasons.map(([state]) => state));
  const state = SEVERITY.find((s) => present.has(s));
  return [state, reasons.map(([, text]) => text)];
}

/** Whether the feed said anything at all. Pure. [state, detail]. */
export function feedState(events, reachable) {
  if (!reachable) {
    return [FEED_UNAVAILABLE,
      'the feed could not be read, so nothing below is a review of anything'];
  }
  if (!(events ?? []).length) {
    return [FEED_UNAVAILABLE,
      'the feed returned no events at all. Audit logging is gated to ' +
      'organizations that have it enabled, so this is not a clean result: ' +
      'it is an unknown one.'];
  }
  return [FEED_OK, `${events.length} event(s) read`];
}

/** Clusters of login.failed inside one window. Pure. */
export function failedLoginBursts(events, windowSeconds = 600, threshold = 5) {
  const rows = (events ?? [])
    .filter((e) => String(e?.type ?? '') === 'login.failed' && e?.when !== null &&
                   e?.when !== undefined)
    .sort((a, b) => a.when - b.when);
  for (let i = 0; i < rows.length; i += 1) {
    const window = rows.slice(i).filter((e) => e.when - rows[i].when <= windowSeconds);
    if (window.length >= threshold) {
      return [[rows[i].when, window.length, rows[i].actorEmail ?? '(no email)']];
    }
  }
  return [];
}

/** The newest timestamp seen, for the next run's cursor. Pure. */
export function watermark(events) {
  const stamps = (events ?? [])
    .map((e) => e?.when)
    .filter((w) => w !== null && w !== undefined);
  return stamps.length ? Math.max(...stamps) : null;
}

/** Whether this entry's project field means anything. Pure. */
export function projectCaveat(event) {
  if ((event ?? {}).source !== 'openai') return null;
  if ((event ?? {}).actorKind === 'api_key') {
    return 'project is not meaningful here: admin actions taken with an ' +
           'Admin API key are attributed to the default project';
  }
  return null;
}

async function getJson(headers, url, who) {
  const res = await fetch(url, { headers });
  if (res.status === 429) {
    throw new Error(`429 from ${who}: this feed declares its own rate limit ` +
                    'with Retry-After. Back off and resume from the stored watermark.');
  }
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from ${who}: the feed needs an ` +
                    'administration credential, and on Anthropic the ' +
                    'read:compliance_activities scope');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function collect(headers, base, path, params, who, cursor = 'after') {
  const rows = [];
  let after = null;
  for (let page = 0; page < 20; page += 1) {
    const url = new URL(base + path);
    for (const [k, v] of params) url.searchParams.append(k, String(v));
    if (after) url.searchParams.set(cursor, after);
    const body = await getJson(headers, url, who);
    rows.push(...(body.data ?? []));
    if (!body.has_more || !body.last_id) return rows;
    after = body.last_id;
  }
  return rows;
}

function report(name, events, roster, options, geography) {
  const [state, detail] = feedState(events, true);
  console.log(`${name}: ${state} (${detail}), roster of ${roster.size} member(s); ` +
              `${geography ? 'country and session rules available'
                           : 'no geography on this feed'}`);
  if (state === FEED_UNAVAILABLE) return 0;

  let findings = 0;
  for (const event of [...events].sort((a, b) => (a.when ?? 0) - (b.when ?? 0))) {
    const [verdict, reasons] = grade(event, roster, options.businessHours,
                                     geography ? options.countries : null);
    if (verdict === REVIEWED) continue;
    findings += 1;
    console.warn(`${verdict.padEnd(19)} ${event.type.padEnd(22)} ` +
                 `${iso(event.when)}  ` +
                 `${(event.actorEmail ?? `(${event.actorKind} actor)`).padEnd(18)} ` +
                 `${(event.actorIp ?? '-').padEnd(15)} ${event.country ?? ''}`);
    for (const reason of reasons) console.warn(`  reason: ${reason}`);
    const caveat = projectCaveat(event);
    if (caveat) console.log(`  note: ${caveat}`);
  }

  for (const [when, count, who] of failedLoginBursts(events)) {
    findings += 1;
    console.warn(`login-failed-burst   ${count} failure(s) within 10 minutes ` +
                 `from ${who}, starting ${iso(when)}`);
  }

  const mark = watermark(events);
  if (mark !== null) {
    console.log(`watermark: store the cursor ${mark} (${iso(mark)}) for the ` +
                `next ${name} run`);
  }
  return findings;
}

async function main() {
  const openaiKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  const anthropicKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!openaiKey && !anthropicKey) {
    console.error('set OPENAI_ADMIN_KEY or ANTHROPIC_ADMIN_KEY, or both; the ' +
                  'Anthropic credential also needs the ' +
                  'read:compliance_activities scope');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const options = {
    businessHours: [Number((process.env.HOURS_FROM || "dummy-hours-from") ?? 7),
                    Number((process.env.HOURS_TO || "dummy-hours-to") ?? 19)],
    countries: String((process.env.COUNTRIES || "dummy-countries") ?? 'US,GB,DE,IE')
      .split(',').map((c) => c.trim()).filter(Boolean),
  };
  const since = Math.floor(Date.now() / 1000) - days * 86400;
  let findings = 0;

  if (openaiKey) {
    const headers = { Authorization: `Bearer ${openaiKey}` };
    const users = await collect(headers, OPENAI, '/organization/users',
                                [['limit', 100]], 'OpenAI');
    const roster = new Set(users.filter((u) => u.email)
      .map((u) => String(u.email).trim().toLowerCase()));
    const raw = await collect(headers, OPENAI, '/organization/audit_logs',
      [['limit', 100], ['effective_at[gte]', since],
       ...OPENAI_EVENTS.map((t) => ['event_types[]', t])], 'OpenAI');
    findings += report('openai', raw.map(normaliseOpenai), roster, options, true);
  }

  if (anthropicKey) {
    const headers = { 'x-api-key': anthropicKey, 'anthropic-version': ANTHROPIC_VERSION };
    const users = await collect(headers, ANTHROPIC, '/organizations/users',
                                [['limit', 1000]], 'Anthropic', 'after_id');
    const roster = new Set(users.filter((u) => u.email)
      .map((u) => String(u.email).trim().toLowerCase()));
    const raw = await collect(headers, ANTHROPIC, '/compliance/activities',
                              [['limit', 100]], 'Anthropic');
    const events = raw.map(normaliseAnthropic).filter((e) => (e.when ?? 0) >= since);
    findings += report('anthropic', events, roster, options, false);
  }

  console.log(`${findings} finding(s)`);
  console.log('the repair is a schedule, not a run: poll from the stored ' +
              'watermark and route these events to somewhere a person looks');
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
