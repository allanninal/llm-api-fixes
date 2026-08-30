/**
 * Find OpenAI vector stores that will delete themselves, and ones that have.
 *
 * Read only. One paged GET against /v1/vector_stores. No request body, and no
 * file_search query is ever run.
 *
 * expires_after is {anchor: "last_active_at", days: N} and the anchor is not a
 * choice, so every policy is an idle timer. Decisions are made on the reported
 * expires_at; last_active_at + days is computed only to be printed as a drift,
 * because which operations reset the anchor is not documented.
 */
const API = 'https://api.openai.com/v1';
const BETA = { 'OpenAI-Beta': 'assistants=v2' };
const DAY = 86400;

/** The only anchor the API supports. */
export const ANCHOR = 'last_active_at';

const FINDINGS = new Set(['expired', 'policy-on-permanent', 'expiring-soon']);

/** The store ids the team treats as permanent. Pure. */
export function idSet(...raw) {
  const out = new Set();
  for (const chunk of raw) {
    if (!chunk) continue;
    const items = Array.isArray(chunk) ? chunk : [chunk];
    for (const item of items) {
      for (const token of String(item ?? '').trim().split(/[,\s]+/)) {
        if (token) out.add(token);
      }
    }
  }
  return out;
}

/** [anchor, days] from expires_after, or null. Pure. */
export function policy(store) {
  const raw = store?.expires_after;
  if (!raw || typeof raw !== 'object') return null;
  const days = Number(raw.days);
  if (!Number.isFinite(days) || Math.trunc(days) <= 0) return null;
  const anchor = String(raw.anchor ?? '').trim().toLowerCase() || ANCHOR;
  return [anchor, Math.trunc(days)];
}

/** expires_at as an integer, or null. Pure. */
export function expiryAt(store) {
  const n = Number(store?.expires_at ?? 0);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : null;
}

/** Seconds since last_active_at, or null. Pure. */
export function idleSeconds(store, now) {
  const last = Number(store?.last_active_at ?? 0);
  if (!Number.isFinite(last) || last <= 0) return null;
  return now - Math.trunc(last);
}

/** reported expires_at minus last_active_at + days. Pure. Never overrides. */
export function driftSeconds(store) {
  const pol = policy(store);
  const reported = expiryAt(store);
  if (!pol || reported === null) return null;
  const last = Number(store?.last_active_at ?? 0);
  if (!Number.isFinite(last) || last <= 0) return null;
  return reported - (Math.trunc(last) + pol[1] * DAY);
}

/** A line about an unexpected anchor, or null. Pure. */
export function anchorNote(store) {
  const pol = policy(store);
  if (pol && pol[0] !== ANCHOR) {
    return `expires_after.anchor is '${pol[0]}' and the only documented value is `
      + `'${ANCHOR}'. Read the reference before treating this as a misconfiguration.`;
  }
  return null;
}

/** Classify one store's clock. Pure. Returns [state, detail]. */
export function expiryState(store, now, permanent = new Set(), noticeDays = 7) {
  const st = store ?? {};
  const sid = String(st.id ?? '');
  const pol = policy(st);
  const reported = expiryAt(st);
  const idle = idleSeconds(st, now);
  const perm = permanent instanceof Set ? permanent : new Set(permanent ?? []);

  if (String(st.status ?? '').trim().toLowerCase() === 'expired') {
    const ago = reported ? ` ${Math.max((now - reported) / DAY, 0).toFixed(0)} day(s) ago` : '';
    return ['expired',
            `expired${ago}. The contained file objects were deleted and are not `
            + 'recoverable.'];
  }

  if (!pol) {
    const size = Number(st.usage_bytes ?? 0);
    return ['permanent',
            `no policy, ${(Number.isFinite(size) ? size / 1048576 : 0).toFixed(1)} `
            + 'MiB retained and billed'];
  }

  const left = reported !== null ? (reported - now) / DAY : null;
  const leftText = left !== null ? `${left.toFixed(1)} day(s) left`
                                 : 'no expires_at reported';

  if (perm.has(sid)) {
    return ['policy-on-permanent',
            `${pol[1]} day idle timer on a store you listed as permanent, ${leftText}`];
  }
  if (left !== null && left <= noticeDays) {
    const idleText = idle ? `, idle for ${(idle / DAY).toFixed(1)} day(s)` : '';
    return ['expiring-soon', `${leftText}${idleText}`];
  }
  return ['scheduled', `${pol[1]} day idle timer, ${leftText}`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'expired') {
    return ['re-ingest into a new store. Clearing the policy on this one changes '
            + 'nothing, because the files it held are already gone.',
            'set the policy you actually want on the new store at creation, and '
            + 'put whatever produced the corpus into source control so the next '
            + 're-ingest is a command rather than an afternoon.'];
  }
  if (state === 'policy-on-permanent') {
    return ['clear it by updating expires_after to null on the store. The listing '
            + 'is a read; the clear is a write and is yours to run.',
            'the anchor is last_active_at and cannot be changed, so a permanent '
            + 'store cannot be expressed as a long policy. It has to be no policy '
            + 'at all.'];
  }
  if (state === 'expiring-soon') {
    return ['decide which this store is before the date. Temporary is fine and '
            + 'needs no change; permanent means clearing the policy now rather '
            + 'than after the files are deleted.',
            'run this check on a schedule shorter than the smallest days value it '
            + 'reports, or it will tell you about the deletion afterwards.'];
  }
  if (state === 'permanent') {
    return ['nothing expires here, which also means nothing is reclaimed. '
            + 'Retained bytes are billed by the hour whether or not anything '
            + 'queries them.'];
  }
  return [];
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}`, ...BETA } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/vector_stores needs a project key`);
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* paged(key, path, params, maxPages = 200) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q);
    const data = page.data ?? [];
    for (const item of data) yield item;
    if (!page.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key for the project that owns '
                  + 'the vector stores');
    process.exitCode = 2;
    return;
  }
  const permanent = idSet((process.env.PERMANENT_VECTOR_STORE_IDS || "dummy-permanent-vector-store-ids"));
  const noticeDays = Number((process.env.NOTICE_DAYS || "dummy-notice-days") ?? 7);

  const stores = [];
  for await (const st of paged(key, '/vector_stores', { limit: 100 })) stores.push(st);
  const withPolicy = stores.filter((st) => policy(st));
  console.log(`${stores.length} store(s) visible to this key, ${withPolicy.length} `
              + 'with an expiration policy');

  const now = Math.floor(Date.now() / 1000);
  let findings = 0;
  for (const store of stores) {
    const sid = store?.id ?? '?';
    const name = store?.name ?? '(unnamed)';
    const [state, detail] = expiryState(store, now, permanent, noticeDays);
    console.log(`${state.padEnd(20)} ${sid} ${name}: ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    const note = anchorNote(store);
    if (note) console.log(`  anchor: ${note}`);
    const drift = driftSeconds(store);
    if (drift !== null && Math.abs(drift) > 3600) {
      console.log(`  drift: reported expires_at is ${(Math.abs(drift) / 3600).toFixed(1)}h `
                  + `${drift > 0 ? 'ahead of' : 'behind'} last_active_at plus the `
                  + 'policy window');
    }
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
