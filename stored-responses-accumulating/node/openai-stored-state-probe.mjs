/**
 * Probe recorded response and conversation ids for retention and volume.
 *
 * Read only. GET /v1/responses/{id}, GET /v1/conversations/{id} and
 * GET /v1/conversations/{id}/items. Nothing is created or deleted.
 *
 * Neither collection has a list endpoint, so this probes the ids you recorded
 * and prints a coverage statement every run.
 *
 * Stored response data is kept for AT LEAST 30 days, which is a floor rather
 * than a deadline. Conversations are retained UNTIL DELETED, and their items
 * are not deleted when the conversation is.
 *
 * This never follows previous_response_id: whether a thread still resolves is
 * a different question and a different script.
 */
import { readFile } from 'node:fs/promises';

const RESPONSES_URL = 'https://api.openai.com/v1/responses';
const CONVERSATIONS_URL = 'https://api.openai.com/v1/conversations';

export const RESPONSE_RETENTION_FLOOR_DAYS = 30;
const ITEM_PAGE = 100;

const FINDINGS = new Set(['retained-past-policy', 'items-outlive-response',
  'thread-unbounded', 'thread-idle', 'probe-unreadable']);

/** Route recorded ids by prefix. Pure. What cannot be routed is kept. */
export function parseRecords(text) {
  const out = { responses: [], conversations: [], unrecognised: [] };
  const seen = new Set();
  for (const line of String(text ?? '').split('\n')) {
    const item = line.split('#')[0].trim();
    if (!item || seen.has(item)) continue;
    seen.add(item);
    if (item.startsWith('resp_')) out.responses.push(item);
    else if (item.startsWith('conv_')) out.conversations.push(item);
    else out.unrecognised.push(item);
  }
  return out;
}

/** One retrieved response, reduced. Pure. Five fields and no chain. */
export function responseRow(body) {
  const row = (body && typeof body === 'object') ? body : {};
  const conversation = (row.conversation && typeof row.conversation === 'object')
    ? row.conversation.id : row.conversation;
  const created = Number(row.created_at ?? 0);
  const metadata = row.metadata;
  return {
    id: String(row.id ?? ''),
    created_at: Number.isFinite(created) ? Math.max(0, Math.trunc(created)) : 0,
    status: String(row.status ?? ''),
    conversation: String(conversation ?? ''),
    metadata_keys: (metadata && typeof metadata === 'object')
      ? Object.keys(metadata).length : 0,
  };
}

/** Count and the two timestamps that bound a thread. Pure. */
export function itemTotals(items) {
  const stamps = [];
  for (const item of items ?? []) {
    if (!item || typeof item !== 'object') continue;
    const at = Number(item.created_at ?? 0);
    if (Number.isFinite(at) && at > 0) stamps.push(Math.trunc(at));
  }
  return {
    count: (items ?? []).length,
    oldest: stamps.length ? Math.min(...stamps) : 0,
    newest: stamps.length ? Math.max(...stamps) : 0,
  };
}

/** Age in days. Pure. The clock is an argument. Null when undatable. */
export function ageDays(when, now) {
  const at = Number(when);
  const ref = Number(now);
  if (!Number.isFinite(at) || !Number.isFinite(ref) || at <= 0) return null;
  return (ref - at) / 86400;
}

/** Grade one stored response against YOUR policy. Pure. */
export function gradeResponse(row, status, now, policyDays) {
  if (status === 404) {
    return ['not-retained',
      'nothing is stored under this id. It was created with store false, or it '
      + 'has already aged out'];
  }
  if (status !== 200) {
    return ['probe-unreadable',
      `HTTP ${status}, so nothing about this id was established`];
  }
  const age = ageDays((row ?? {}).created_at, now);
  if (age === null) {
    return ['undatable',
      'stored, but it carried no usable created_at, so its age cannot be graded'];
  }
  const conversation = String((row ?? {}).conversation ?? '');
  if (age > Number(policyDays)) {
    const tail = conversation
      ? `, and its items were added to conversation ${conversation}, which is `
        + 'retained until deleted'
      : '';
    return ['retained-past-policy',
      `still readable ${age.toFixed(1)} day(s) after creation, past your `
      + `${Math.trunc(policyDays)} day policy. Retention is documented as at least `
      + `${RESPONSE_RETENTION_FLOOR_DAYS} days, so that is a floor and not a `
      + `deadline${tail}`];
  }
  if (conversation) {
    return ['items-outlive-response',
      `${age.toFixed(1)} day(s) old and inside your policy, but its items were `
      + `added to conversation ${conversation}, which is retained until deleted`];
  }
  return ['within-policy',
    `stored, ${age.toFixed(1)} day(s) old, inside your ${Math.trunc(policyDays)} `
    + 'day policy'];
}

/** Grade one conversation on volume first, then on idleness. Pure. */
export function gradeConversation(row, totals, status, now, policyDays, maxItems) {
  if (status === 404) {
    return ['not-retained',
      'no conversation under this id, so it has already been deleted'];
  }
  if (status !== 200) {
    return ['probe-unreadable',
      `HTTP ${status}, so nothing about this id was established`];
  }
  const tot = totals ?? { count: 0, oldest: 0, newest: 0 };
  if (Number(tot.count ?? 0) > Number(maxItems)) {
    return ['thread-unbounded',
      `${Number(tot.count)} item(s) and no TTL, so every turn on this thread `
      + 'carries them as input'];
  }
  const idle = ageDays(tot.newest, now);
  if (idle !== null && idle > Number(policyDays)) {
    return ['thread-idle',
      `last item ${idle.toFixed(1)} day(s) ago, past your ${Math.trunc(policyDays)} `
      + 'day policy, and conversations are retained until deleted'];
  }
  if (idle === null) {
    return ['thread-undatable',
      `${Number(tot.count ?? 0)} item(s), none of which carried a usable created_at`];
  }
  return ['thread-within-policy',
    `${Number(tot.count ?? 0)} item(s), last active ${idle.toFixed(1)} day(s) ago`];
}

/** The sentence that has to appear on every run. Pure. */
export function coverageNote(records) {
  const r = records ?? {};
  const responses = (r.responses ?? []).length;
  const conversations = (r.conversations ?? []).length;
  const unrecognised = (r.unrecognised ?? []).length;
  return `${responses + conversations + unrecognised} id(s) supplied: `
    + `${responses} response(s), ${conversations} conversation(s), `
    + `${unrecognised} unroutable. Neither /v1/responses nor /v1/conversations `
    + 'has a list endpoint, so this is your records and not your account';
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const itemsFirst = 'delete the items first with DELETE /v1/conversations/'
    + '{conversation_id}/items/{item_id}, then the conversation. Deleting the '
    + 'conversation does not delete its items.';
  if (state === 'retained-past-policy') {
    return ['DELETE /v1/responses/{response_id} for what you no longer need, and '
      + 'pass store false on calls carrying regulated data.',
    'keep an id ledger with a created_at. It is the only inventory that can '
      + 'exist, because neither collection can be listed.'];
  }
  if (state === 'items-outlive-response') {
    return [`deleting the response is not enough here. ${itemsFirst}`];
  }
  if (state === 'thread-unbounded') {
    return ['start a fresh conversation seeded with a summary once a thread gets '
      + 'long, so input tokens stop compounding.', itemsFirst];
  }
  if (state === 'thread-idle') return [itemsFirst];
  if (state === 'probe-unreadable') {
    return ['the key could not read this id. Check that it belongs to the project '
      + 'that created the object before concluding anything about retention.'];
  }
  if (state === 'unrecognised-id') {
    return ['route it by hand, or drop it. An id this script cannot classify is a '
      + 'hole in a coverage figure that is already bounded by your own records.'];
  }
  return [];
}

async function getJson(url, key, params) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) {
    target.searchParams.set(k, String(v));
  }
  try {
    const res = await fetch(target, { headers: { Authorization: `Bearer ${key}` } });
    const body = await res.json().catch(() => null);
    return [res.status, body];
  } catch {
    return [null, null];
  }
}

async function walkItems(conversationId, key, maxPages) {
  const url = `${CONVERSATIONS_URL}/${conversationId}/items`;
  const items = [];
  let cursor = null;
  let pages = 0;
  while (pages < maxPages) {
    const params = { limit: ITEM_PAGE, order: 'asc' };
    if (cursor) params.after = cursor;
    const [status, body] = await getJson(url, key, params);
    if (status !== 200 || !body || typeof body !== 'object') return [items, false];
    const data = body.data ?? [];
    pages += 1;
    items.push(...data);
    if (!data.length || body.has_more === false) return [items, true];
    cursor = data[data.length - 1]?.id;
    if (!cursor) return [items, true];
  }
  return [items, false];
}

function args(argv) {
  const out = { policyDays: 30, maxItems: 500, maxItemPages: 50 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--records') out.records = argv[i += 1];
    else if (argv[i] === '--policy-days') out.policyDays = Number(argv[i += 1]);
    else if (argv[i] === '--max-items') out.maxItems = Number(argv[i += 1]);
    else if (argv[i] === '--max-item-pages') out.maxItemPages = Number(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only. Every '
      + 'call is a GET of a response, a conversation or its items');
    process.exitCode = 2;
    return;
  }
  if (!opts.records) {
    console.error('usage: --records <file> [--policy-days 30] [--max-items 500]');
    process.exitCode = 2;
    return;
  }
  let records;
  try {
    records = parseRecords(await readFile(opts.records, 'utf8'));
  } catch (err) {
    console.error(`could not read ${opts.records}: ${err.message}`);
    process.exitCode = 2;
    return;
  }
  const probed = records.responses.length + records.conversations.length;
  if (!probed) {
    console.error(`no resp_ or conv_ ids in ${opts.records}. Neither collection `
      + 'can be listed, so the ids have to come from your own records');
    process.exitCode = 2;
    return;
  }

  const now = Math.trunc(Date.now() / 1000);
  console.log(coverageNote(records));
  let findings = 0;

  for (const id of records.responses) {
    const [status, body] = await getJson(`${RESPONSES_URL}/${id}`, key);
    const [state, detail] = gradeResponse(responseRow(body), status, now,
                                          opts.policyDays);
    console.log(`${state.padEnd(22)} ${id}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  for (const id of records.conversations) {
    const [status, body] = await getJson(`${CONVERSATIONS_URL}/${id}`, key);
    let totals = null;
    if (status === 200) {
      const [items, complete] = await walkItems(id, key, opts.maxItemPages);
      totals = itemTotals(items);
      if (!complete) {
        console.log(`${'items-incomplete'.padEnd(22)} ${id}: the item listing `
          + `stopped early, so ${totals.count} is a floor`);
      }
    }
    const [state, detail] = gradeConversation(body, totals, status, now,
                                              opts.policyDays, opts.maxItems);
    console.log(`${state.padEnd(22)} ${id}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  for (const other of records.unrecognised) {
    console.log(`${'unrecognised-id'.padEnd(22)} ${other}: neither a resp_ nor a `
      + 'conv_ id, so it was not probed');
  }

  console.log(`${probed + records.unrecognised.length} supplied, ${probed} probed, `
    + `${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
