/**
 * Find Claude Code actors whose sessions never read a cached prefix.
 *
 * Read only. One paged GET per UTC day against the Claude Code usage report
 * with an Admin API key.
 *
 * This is a different report from the messages usage report: its unit is an
 * actor and a day, and it cannot be joined to the other by any field. It also
 * covers Claude Code on the Claude API only, so Bedrock, Google Cloud, Foundry
 * and Claude Platform on AWS usage is simply absent.
 *
 * No savings figure is printed. The report does not say how much of
 * tokens.input was reusable prefix, and that ratio is the whole calculation.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const FINDINGS = new Set(['no-cache-at-all', 'writes-never-read', 'thin-cache']);

/** Who the record belongs to. Pure. Both actor shapes, plus neither. */
export function actorName(record) {
  const actor = record?.actor;
  if (!actor || typeof actor !== 'object') return 'unattributed';
  for (const field of ['email_address', 'api_key_name']) {
    const value = String(actor[field] ?? '').trim();
    if (value) return value;
  }
  return 'unattributed';
}

/** Hide the local part of an email address. Pure. Non-emails pass through. */
export function mask(name) {
  const text = String(name ?? '').trim();
  if (!text.includes('@')) return text || 'unattributed';
  const at = text.indexOf('@');
  const local = text.slice(0, at);
  if (!local) return text;
  return `${local[0]}***${text.slice(at)}`;
}

/** The four token counts off one model_breakdown entry. Pure. */
export function tokensOf(entry) {
  const tokens = entry?.tokens && typeof entry.tokens === 'object' ? entry.tokens : {};
  const out = {};
  for (const field of ['input', 'output', 'cache_read', 'cache_creation']) {
    const n = Number(tokens[field] ?? 0);
    out[field] = Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0;
  }
  return out;
}

/**
 * estimated_cost.amount in cents. Pure. 0 when unreadable.
 * Kept as a number of cents rather than dollars so the rounding happens once,
 * at the point of printing, instead of on every addition.
 */
export function costCents(entry) {
  const cost = entry?.estimated_cost && typeof entry.estimated_cost === 'object'
    ? entry.estimated_cost : {};
  const n = Number(cost.amount ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/** Fold every record into one row per actor. Pure. Sums across model_breakdown. */
export function fold(pages) {
  const rows = {};
  for (const page of pages ?? []) {
    for (const record of page?.data ?? []) {
      if (!record || typeof record !== 'object') continue;
      const who = actorName(record);
      const row = rows[who] ?? { sessions: 0, days: 0, input: 0, output: 0,
                                 cache_read: 0, cache_creation: 0, cents: 0,
                                 models: new Set() };
      rows[who] = row;
      row.days += 1;
      const core = record.core_metrics && typeof record.core_metrics === 'object'
        ? record.core_metrics : {};
      const sessions = Number(core.num_sessions ?? 0);
      if (Number.isFinite(sessions)) row.sessions += Math.max(0, Math.trunc(sessions));
      for (const entry of record.model_breakdown ?? []) {
        const counts = tokensOf(entry);
        for (const [field, value] of Object.entries(counts)) row[field] += value;
        row.cents += costCents(entry);
        const model = String(entry?.model ?? '').trim();
        if (model) row.models.add(model);
      }
    }
  }
  return rows;
}

/**
 * Share of an actor's input that was read back from cache. Pure.
 * Writes are not in the denominator: they are a cost, not a hit.
 */
export function readShare(row) {
  const reads = Math.max(0, Number(row?.cache_read ?? 0));
  const fresh = Math.max(0, Number(row?.input ?? 0));
  const total = reads + fresh;
  if (total <= 0) return 0;
  return reads / total;
}

/** Cents per session for one actor. Pure. Null when there are no sessions. */
export function costPerSession(row) {
  const sessions = Math.max(0, Number(row?.sessions ?? 0));
  if (sessions <= 0) return null;
  return Number(row?.cents ?? 0) / sessions;
}

/** Classify one actor's cache behaviour. Pure. Returns [state, detail]. */
export function verdict(row, minSessions = 2, minInput = 100_000, floor = 0.1) {
  const data = row ?? {};
  const sessions = Math.max(0, Number(data.sessions ?? 0));
  const reads = Math.max(0, Number(data.cache_read ?? 0));
  const writes = Math.max(0, Number(data.cache_creation ?? 0));
  const fresh = Math.max(0, Number(data.input ?? 0));

  if (sessions < minSessions) {
    return ['too-few-sessions',
      `${sessions} session(s) in the window: there was no earlier turn for a ` +
      'prefix to be read back from, so a zero here is arithmetic rather than ' +
      'a finding'];
  }
  if (reads + fresh < minInput) {
    return ['low-volume',
      `${sessions} session(s) and ${reads + fresh} input token(s), too few to ` +
      'conclude anything'];
  }

  const share = readShare(data);
  if (reads === 0 && writes === 0) {
    return ['no-cache-at-all',
      `${sessions} session(s), 0% of input read from cache, and no cache ` +
      'writes either: the prefix is never being cached at all'];
  }
  if (reads === 0) {
    return ['writes-never-read',
      `${sessions} session(s), 0% read with ${(writes / 1e6).toFixed(1)}M ` +
      'token(s) written: entries are being created at a premium and never matched'];
  }
  if (share < floor) {
    return ['thin-cache',
      `${sessions} session(s), ${(share * 100).toFixed(0)}% of input read ` +
      `from cache, under the floor of ${(floor * 100).toFixed(0)}%`];
  }
  return ['cached',
    `${sessions} session(s), ${(share * 100).toFixed(0)}% of input read from cache`];
}

/** The repair for one classified actor. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'no-cache-at-all') {
    return [
      'check whether these sessions are one prompt each. A prefix is only ' +
      'reusable across turns of the same session, so a fresh session per ' +
      'question pays full rate for the project context, the tool definitions ' +
      'and every file already read.',
      'continuing a session rather than starting one is the whole fix, and it ' +
      'is a habit rather than a setting.',
    ];
  }
  if (state === 'writes-never-read') {
    return [
      'entries are being written and never matched, so something ahead of the ' +
      'stable block is changing between turns.',
      'this is the more expensive of the two zeros: cache writes cost more ' +
      'than plain input, so the current state is worse than not caching at all.',
    ];
  }
  if (state === 'thin-cache') {
    return ['some turns are matching and most are not. Look for a mix of long ' +
            'sessions and one-shot invocations under the same actor before ' +
            'concluding the prefix is unstable.'];
  }
  return [];
}

/** The UTC dates to request, newest first. Pure. Today is excluded. */
export function dayStrings(days, today = new Date()) {
  const out = [];
  const count = Math.max(1, Math.trunc(Number(days) || 1));
  for (let n = 1; n <= count; n += 1) {
    const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(),
                                today.getUTCDate() - n));
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of params) url.searchParams.append(k, v);
  const res = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs ` +
                    'an Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 7);
  const showActors = (process.env.SHOW_ACTORS || "dummy-show-actors") === '1';
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const dates = dayStrings(days);
  const collected = [];
  for (const day of dates) {
    const base = [['starting_at', day], ['limit', '1000']];
    let params = base;
    for (;;) {
      const page = await get(admin, '/organizations/usage_report/claude_code', params);
      collected.push(page);
      if (!page?.has_more || !page?.next_page) break;
      params = [...base, ['page', page.next_page]];
    }
  }

  const rows = fold(collected);
  const actors = Object.keys(rows);
  if (actors.length === 0) {
    console.log(`no Claude Code records over ${dates.length} day(s). This ` +
                'report covers Claude Code on the Claude API only: Bedrock, ' +
                'Google Cloud, Foundry and Claude Platform on AWS usage is not here.');
    return;
  }

  let bad = 0;
  for (const who of actors.sort((a, b) => rows[b].cents - rows[a].cents)) {
    const row = rows[who];
    const [state, detail] = verdict(row, 2);
    const label = showActors ? who : mask(who);
    const line = `${state.padEnd(20)} ${label.padEnd(22)} ${detail}, ` +
                 `$${(row.cents / 100).toFixed(2)}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      for (const repair of repairLines(state)) console.warn(`  repair: ${repair}`);
    } else if (showAll || state !== 'cached') {
      console.log(line);
    }
  }

  console.log(`${actors.length} actor(s) over ${dates.length} day(s), ${bad} finding(s)`);
  console.log('no savings figure: the report does not say how much of ' +
              'tokens.input was reusable prefix, and that ratio is the whole ' +
              'calculation');
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
