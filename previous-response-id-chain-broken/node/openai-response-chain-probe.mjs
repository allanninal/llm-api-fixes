/**
 * Walk recorded previous_response_id chains and find the links already gone.
 *
 * Read only. One GET of /v1/responses/{response_id} per link. No completion is
 * created, nothing is stored and nothing is deleted.
 *
 * Response objects are saved for 30 days by default, so a chain is only as
 * durable as its oldest surviving link. A response attached to a conversation
 * has its items persisted with no 30 day TTL, which is the repair.
 *
 * /v1/responses has no list endpoint, so the ids come from your own records.
 */
import { readFile } from 'node:fs/promises';

const BASE_URL = 'https://api.openai.com/v1/responses';

export const RETENTION_DAYS = 30;

const FINDINGS = new Set(['chain-broken', 'chain-expiring', 'chain-unreadable']);

/** Response ids from a file. Pure. Blanks, comments and repeats dropped. */
export function parseIds(text) {
  const seen = [];
  for (const line of String(text ?? '').split('\n')) {
    const item = line.split('#')[0].trim();
    if (item && !seen.includes(item)) seen.push(item);
  }
  return seen;
}

/** One retrieved response, reduced. Pure. Four fields and no invention. */
export function linkRow(body) {
  const row = (body && typeof body === 'object') ? body : {};
  const conversation = (row.conversation && typeof row.conversation === 'object')
    ? row.conversation.id : row.conversation;
  const created = Number(row.created_at ?? 0);
  return {
    id: String(row.id ?? ''),
    created_at: Number.isFinite(created) ? Math.trunc(created) : 0,
    previous_response_id: String(row.previous_response_id ?? ''),
    conversation: String(conversation ?? ''),
    status: String(row.status ?? ''),
  };
}

/** Age of one link in days. Pure. The clock is an argument. */
export function ageDays(createdAt, now) {
  const created = Number(createdAt);
  const at = Number(now);
  if (!Number.isFinite(created) || !Number.isFinite(at) || created <= 0) return null;
  return (at - created) / 86400;
}

/** The link that decides the chain. Pure. Null for an empty chain. */
export function oldestLink(chain) {
  const usable = (chain ?? []).filter((row) => Number(row?.created_at ?? 0) > 0);
  if (!usable.length) return null;
  return usable.reduce((a, b) => (Number(a.created_at) <= Number(b.created_at) ? a : b));
}

/** Days left on the oldest link. Pure. Null when nothing is datable. */
export function runwayDays(chain, now, retention = RETENTION_DAYS) {
  const row = oldestLink(chain);
  if (!row) return null;
  const age = ageDays(row.created_at, now);
  return age === null ? null : retention - age;
}

/** Grade one walked chain. Pure. Returns [state, detail]. */
export function classifyChain(head, chain, gap, unreadable, truncated, now, warnDays) {
  if (unreadable) {
    return ['chain-unreadable',
      `${head}: ${unreadable}, so nothing about this chain was established`];
  }
  if (gap) {
    if ((chain ?? []).length) {
      return ['chain-broken',
        `${head}: the parent ${gap} no longer resolves, so the next turn on this `
        + 'thread will 404'];
    }
    return ['chain-broken',
      `${head}: this id itself does not resolve. It has either aged out of the `
      + '30 day retention or was never stored'];
  }
  if (!(chain ?? []).length) return ['nothing-walked', `${head}: no links were read`];

  const conversations = new Set(chain.map((row) => row.conversation ?? ''));
  if (!conversations.has('')) {
    return ['conversation-backed',
      `${head}: items attached to a conversation are persisted with no 30 day TTL`];
  }

  const left = runwayDays(chain, now);
  if (left === null) {
    return ['undatable',
      `${head}: no link carried a usable created_at, so the runway cannot be computed`];
  }
  if (left <= 0) {
    return ['chain-broken',
      `${head}: the oldest link is past the documented ${RETENTION_DAYS} day `
      + 'retention and is only resolving on borrowed time'];
  }
  if (left <= warnDays) {
    const row = oldestLink(chain);
    return ['chain-expiring',
      `${head}: the oldest link is ${ageDays(row.created_at, now).toFixed(1)} days `
      + `old, so this chain has about ${left.toFixed(1)} days of the documented `
      + `${RETENTION_DAYS} day retention left`];
  }
  if (truncated) {
    return ['chain-unfinished',
      `${head}: stopped at the hop limit before reaching a root, so the oldest `
      + 'link was never seen'];
  }
  return ['chain-intact',
    `${head}: walked to a root with ${left.toFixed(1)} days left on the oldest link`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const move = 'move this thread onto a conversation object, whose items are '
    + 'persisted with no 30 day TTL, or keep the full message history in your '
    + 'own store and replay it.';
  if (state === 'chain-broken') {
    return ['fall back to replaying local history for this thread, and stop '
      + 'chaining from an id you did not verify.', move];
  }
  if (state === 'chain-expiring') {
    return [move, 'until then, verify the parent resolves before continuing an '
      + 'old thread rather than discovering it inside a user request.'];
  }
  if (state === 'chain-unreadable') {
    return ['the key could not read this response. Check that it belongs to the '
      + 'project that created it before concluding anything about retention.'];
  }
  if (state === 'chain-unfinished') {
    return ['raise --max-hops for this thread. A chain graded without reaching '
      + 'its oldest link has not been graded.'];
  }
  if (state === 'undatable') {
    return ['the links resolved but carried no created_at, which is odd enough '
      + 'to read one of them by hand before trusting the rest.'];
  }
  return [];
}

async function retrieve(responseId, key) {
  try {
    const res = await fetch(`${BASE_URL}/${responseId}`,
      { headers: { Authorization: `Bearer ${key}` } });
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    return [res.status, body];
  } catch {
    return [null, null];
  }
}

async function walk(head, key, maxHops) {
  const chain = [];
  let current = head;
  for (let hop = 0; hop < maxHops; hop += 1) {
    const [status, body] = await retrieve(current, key);
    if (status === 404) return [chain, current, '', false];
    if (status !== 200) return [chain, '', `HTTP ${status} reading ${current}`, false];
    const row = linkRow(body);
    chain.push(row);
    if (!row.previous_response_id) return [chain, '', '', false];
    current = row.previous_response_id;
  }
  return [chain, '', '', true];
}

function args(argv) {
  const out = { maxHops: 20, warnDays: 5 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--ids') out.ids = argv[i += 1];
    else if (argv[i] === '--max-hops') out.maxHops = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--warn-days') out.warnDays = Number(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only. Every '
      + 'call is a GET of /v1/responses/{response_id}');
    process.exitCode = 2;
    return;
  }
  if (!opts.ids) {
    console.error('usage: --ids <file> [--max-hops 20] [--warn-days 5]');
    process.exitCode = 2;
    return;
  }
  let heads;
  try {
    heads = parseIds(await readFile(opts.ids, 'utf8'));
  } catch (err) {
    console.error(`could not read ${opts.ids}: ${err.message}`);
    process.exitCode = 2;
    return;
  }
  if (!heads.length) {
    console.error(`no response ids in ${opts.ids}. /v1/responses cannot be `
      + 'listed, so the chains have to start from ids you recorded');
    process.exitCode = 2;
    return;
  }

  const now = Math.trunc(Date.now() / 1000);
  let findings = 0;
  for (const head of heads) {
    const [chain, gap, unreadable, truncated] = await walk(head, key, opts.maxHops);
    const row = oldestLink(chain);
    if (row) {
      console.log(`${head.padEnd(10)} chain of ${chain.length}, oldest ${row.id}, `
        + `${(ageDays(row.created_at, now) ?? 0).toFixed(1)} days old`);
    } else {
      console.log(`${head.padEnd(10)} chain of ${chain.length}`);
    }
    const [state, detail] = classifyChain(head, chain, gap, unreadable, truncated,
                                          now, opts.warnDays);
    console.log(`${state.padEnd(20)} ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }
  console.log(`${heads.length} chain(s) walked, ${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
