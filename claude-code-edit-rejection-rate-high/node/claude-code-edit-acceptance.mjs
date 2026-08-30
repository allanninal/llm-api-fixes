/**
 * Find Claude Code actors whose edit proposals are mostly rejected.
 *
 * Read only. One paged GET per UTC day against the Claude Code usage report
 * with an Admin API key.
 *
 * Every rejected proposal was fully generated and fully billed before it was
 * displayed. The acceptance rate and the estimated cost are printed as two
 * separate readings and are never multiplied: the report carries no
 * per-proposal token counts, so the share of spend that was discarded is not a
 * number this API can support.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

// The tools that propose a change a person then keeps or discards. Counted
// apart rather than averaged, because they fail for different reasons.
const EDIT_TOOLS = ['edit_tool', 'multi_edit_tool', 'write_tool',
                    'notebook_edit_tool'];

const FINDINGS = new Set(['rejected-more-than-kept', 'low-acceptance']);

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

/**
 * Accepted and rejected counts per edit tool on one record. Pure.
 * A tool nobody used is omitted rather than zeroed.
 */
export function actionsOf(record) {
  const actions = record?.tool_actions && typeof record.tool_actions === 'object'
    ? record.tool_actions : {};
  const out = {};
  for (const tool of EDIT_TOOLS) {
    const row = actions[tool];
    if (!row || typeof row !== 'object') continue;
    const counts = {};
    for (const field of ['accepted', 'rejected']) {
      const n = Number(row[field] ?? 0);
      counts[field] = Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0;
    }
    if (counts.accepted || counts.rejected) out[tool] = counts;
  }
  return out;
}

/** Fold every record into one row per actor. Pure. Productivity travels with the rate. */
export function fold(pages) {
  const rows = {};
  for (const page of pages ?? []) {
    for (const record of page?.data ?? []) {
      if (!record || typeof record !== 'object') continue;
      const who = actorName(record);
      const row = rows[who] ?? { tools: {}, days: 0, sessions: 0, commits: 0,
                                 prs: 0, added: 0, removed: 0, cents: 0 };
      rows[who] = row;
      row.days += 1;
      for (const [tool, counts] of Object.entries(actionsOf(record))) {
        const into = row.tools[tool] ?? { accepted: 0, rejected: 0 };
        into.accepted += counts.accepted;
        into.rejected += counts.rejected;
        row.tools[tool] = into;
      }

      const core = record.core_metrics && typeof record.core_metrics === 'object'
        ? record.core_metrics : {};
      for (const [field, key] of [['num_sessions', 'sessions'],
                                  ['commits_by_claude_code', 'commits'],
                                  ['pull_requests_by_claude_code', 'prs']]) {
        const n = Number(core[field] ?? 0);
        if (Number.isFinite(n)) row[key] += Math.max(0, Math.trunc(n));
      }
      const lines = core.lines_of_code && typeof core.lines_of_code === 'object'
        ? core.lines_of_code : {};
      for (const key of ['added', 'removed']) {
        const n = Number(lines[key] ?? 0);
        if (Number.isFinite(n)) row[key] += Math.max(0, Math.trunc(n));
      }

      for (const entry of record.model_breakdown ?? []) {
        const cost = entry?.estimated_cost && typeof entry.estimated_cost === 'object'
          ? entry.estimated_cost : {};
        const n = Number(cost.amount ?? 0);
        if (Number.isFinite(n)) row.cents += n;
      }
    }
  }
  return rows;
}

/** Accepted and rejected across every edit tool for one actor. Pure. */
export function totals(row) {
  let accepted = 0;
  let rejected = 0;
  for (const counts of Object.values(row?.tools ?? {})) {
    accepted += Math.max(0, Number(counts?.accepted ?? 0));
    rejected += Math.max(0, Number(counts?.rejected ?? 0));
  }
  return [accepted, rejected];
}

/**
 * accepted / (accepted + rejected). Pure. Null when nothing was proposed.
 * Null rather than 0: an actor who proposed nothing has no rate, and zero
 * would put them at the top of a list of the worst.
 */
export function acceptance(counts) {
  const accepted = Math.max(0, Number(counts?.accepted ?? 0));
  const rejected = Math.max(0, Number(counts?.rejected ?? 0));
  const total = accepted + rejected;
  if (total <= 0) return null;
  return accepted / total;
}

/** The lowest-scoring tool with enough volume to mean it. Pure. Null when none. */
export function worstTool(row, minProposals = 10) {
  let worst = null;
  for (const [tool, counts] of Object.entries(row?.tools ?? {}).sort()) {
    const total = Math.max(0, Number(counts?.accepted ?? 0))
      + Math.max(0, Number(counts?.rejected ?? 0));
    if (total < minProposals) continue;
    const rate = acceptance(counts);
    if (rate === null) continue;
    if (worst === null || rate < worst[1]) worst = [tool, rate];
  }
  return worst;
}

/** Classify one actor's acceptance. Pure. Returns [state, detail]. */
export function verdict(row, minProposals = 20, keepFloor = 0.5, thin = 0.7) {
  const [accepted, rejected] = totals(row);
  const total = accepted + rejected;
  if (total < minProposals) {
    return ['too-few-proposals',
      `${total} proposal(s), under the floor of ${minProposals}: a bad ` +
      'afternoon is not a pattern'];
  }
  const rate = acceptance({ accepted, rejected });
  const worst = worstTool(row);
  const tail = worst === null ? ''
    : `; worst tool ${worst[0]} at ${(worst[1] * 100).toFixed(0)}%`;

  if (rate < keepFloor) {
    return ['rejected-more-than-kept',
      `${(rate * 100).toFixed(0)}% accepted over ${total} proposal(s)${tail}: ` +
      'a majority of the diffs shown were discarded after being generated and billed'];
  }
  if (rate < thin) {
    return ['low-acceptance',
      `${(rate * 100).toFixed(0)}% accepted over ${total} proposal(s)${tail}`];
  }
  return ['healthy',
    `${(rate * 100).toFixed(0)}% accepted over ${total} proposal(s)${tail}`];
}

/** The repair for one classified actor. Pure. A conversation, not a change. */
export function repairLines(state, row) {
  if (!FINDINGS.has(state)) return [];
  const commits = Math.max(0, Number(row?.commits ?? 0));
  const lines = [
    'review project setup for these repositories: a CLAUDE.md context file so ' +
    'the model knows where things live, and narrower task scoping so a ' +
    'proposal is small enough to be judged.',
    'check the model and effort level against the work. A frontier model on a ' +
    'mechanical edit produces confident, wide diffs that get rejected on ' +
    'scope rather than on correctness.',
  ];
  if (commits > 0) {
    lines.push(`this actor still landed ${commits} commit(s) in the window, so ` +
               'the tool is producing accepted work as well. Read the rate as ' +
               'a cost per accepted change, not as a failure.');
  } else {
    lines.push('no commits landed through Claude Code in the window, so there ' +
               'is no accepted work to weigh the rejections against.');
  }
  return lines;
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
  const days = Number((process.env.DAYS || "dummy-days") ?? 14);
  const minProposals = Number((process.env.MIN_PROPOSALS || "dummy-min-proposals") ?? 20);
  const showActors = (process.env.SHOW_ACTORS || "dummy-show-actors") === '1';

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
                'report covers Claude Code on the Claude API only.');
    return;
  }

  let bad = 0;
  for (const who of actors.sort((a, b) => rows[b].cents - rows[a].cents)) {
    const row = rows[who];
    const [state, detail] = verdict(row, minProposals);
    const label = showActors ? who : mask(who);
    const line = `${state.padEnd(24)} ${label.padEnd(20)} ${detail}, ` +
                 `$${(row.cents / 100).toFixed(2)}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      for (const repair of repairLines(state, row)) console.warn(`  repair: ${repair}`);
    } else {
      console.log(line);
    }
  }

  console.log(`${actors.length} actor(s) over ${dates.length} day(s), ${bad} finding(s)`);
  console.log('the rate and the cost are separate readings: no per-proposal ' +
              'token counts exist to join them, so the share of spend that was ' +
              'discarded is not a number this API can support');
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
