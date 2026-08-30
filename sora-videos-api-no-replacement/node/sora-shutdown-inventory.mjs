/**
 * Inventory a capability being withdrawn, with no successor to move to.
 *
 * Read only. Every request is a GET: the model objects for the five ids the
 * deprecation table names, the video listing, and the cost report. Nothing
 * here renders a video and no request in this script creates anything.
 *
 * The deprecation table lists no replacement for any Sora id, so REPLACEMENTS
 * below is empty on purpose and a test keeps it empty. And every asset carries
 * its own expires_at, so each file has two deadlines and needs the earlier one.
 */
export const API = 'https://api.openai.com/v1';

// Announced 24 March 2026. Published, and also readable as shutdown_date.
export const SHUTDOWN = '2026-09-24';

export const SORA_IDS = ['sora-2', 'sora-2-pro', 'sora-2-2025-10-06',
  'sora-2-2025-12-08', 'sora-2-pro-2025-10-06'];

// Empty on purpose, and kept empty by a test. What is being withdrawn is a
// capability and not a model, so filling this in with the closest-looking id
// would make the script lie confidently.
export const REPLACEMENTS = {};

const FINDINGS = new Set(['shutdown-dated', 'past-shutdown', 'already-gone',
  'already-expired', 'expires-first', 'outlives-the-endpoint',
  'no-asset-expiry', 'video-spend-accruing']);

const REPAIRS = {
  'shutdown-dated':
    'remove the /v1/videos code path and the sora-2 constants. This is a '
    + 'capability leaving the API, not a model changing name, so the decision '
    + 'is a third-party provider or dropping the feature.',
  'past-shutdown':
    'the date has passed. Anything still calling this path is returning 404 to '
    + 'somebody right now.',
  'already-gone':
    'this id no longer resolves, so the removal is already overdue for '
    + 'whatever still names it.',
  'already-expired':
    'these bytes are gone and only the metadata row is left. If the render '
    + 'mattered, it has to be regenerated before the endpoint closes.',
  'expires-first':
    'download these before their own expiry, which lands sooner than the '
    + 'endpoint shutdown. This is the front of the queue.',
  'outlives-the-endpoint':
    'download these before the shutdown. Their own expiry is later, which is '
    + 'irrelevant once there is no endpoint left to serve them.',
  'no-asset-expiry':
    'no expiry of their own does not mean no deadline. They inherit the '
    + "endpoint's, so they need downloading like everything else.",
  'video-spend-accruing':
    'this is a live feature with money moving through it, not a branch '
    + 'somebody forgot. Whoever owns the customer-facing promise of video '
    + 'generation needs the date before engineering picks a plan.',
};

const day = (iso) => Date.parse(`${iso}T00:00:00Z`);

/** Whole days from today to a date. Pure. Negative once it has passed. */
export function daysLeft(today, when = SHUTDOWN) {
  return Math.round((day(String(when)) - day(String(today))) / 86400000);
}

/** A unix second stamp as a UTC day, or null. Pure. */
export function isoDay(stamp) {
  if (stamp === null || stamp === undefined || stamp === '' || stamp === 0) return null;
  const n = Number(stamp);
  if (!Number.isFinite(n)) return null;
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10);
}

/** The documented successor for one id. Pure. Returns undefined, every time. */
export function replacementFor(modelId) {
  return REPLACEMENTS[String(modelId)];
}

/** Grade one model id. Pure. [state, detail]. */
export function modelVerdict(modelId, status, shutdownDate, today) {
  if (status === null || status === undefined) {
    return ['unreachable', `no response for ${modelId}`];
  }
  const s = Number(status);
  if (s === 404) {
    return ['already-gone',
      `${modelId} no longer resolves, so it is out of the model list already`];
  }
  if (s !== 200) {
    return ['unreadable', `${s} for ${modelId}, so nothing can be read about it`];
  }
  if (!shutdownDate) {
    return ['no-date-from-api',
      'the model object carried no shutdown_date, so the published table is '
      + `the only source and it says ${SHUTDOWN}`];
  }
  const left = daysLeft(today, shutdownDate);
  if (left < 0) {
    return ['past-shutdown', `shutdown_date ${shutdownDate}, which was ${-left} day(s) ago`];
  }
  return ['shutdown-dated', `shutdown_date ${shutdownDate}, ${left} day(s) away`];
}

/** The earlier of an asset's two clocks. Pure. [state, deadline, detail]. */
export function assetDeadline(expiresIso, today, when = SHUTDOWN) {
  const t = String(today);
  const w = String(when);
  if (!expiresIso) {
    return ['no-asset-expiry', w,
      `no expiry of its own, so it dies with the endpoint on ${w}`];
  }
  const e = String(expiresIso);
  if (e <= t) {
    return ['already-expired', e, `expired on ${e}, so the bytes are already unreachable`];
  }
  if (e < w) {
    return ['expires-first', e,
      `expires ${e}, which is ${daysLeft(e, w)} day(s) before the endpoint closes`];
  }
  return ['outlives-the-endpoint', w,
    `its own expiry is ${e}, so the endpoint closes first on ${w}`];
}

/** Sum the video line items. Pure. [state, total, detail]. */
export function spendVerdict(rows, days) {
  let total = 0;
  for (const [name, amount] of rows || []) {
    const text = String(name ?? '').toLowerCase();
    if (text.includes('video') || text.includes('sora')) total += Number(amount) || 0;
  }
  if (total > 0) {
    return ['video-spend-accruing', total,
      `$${total.toFixed(2)} on video line items in the last ${days} day(s), which `
      + 'is a live feature rather than a branch somebody forgot'];
  }
  return ['no-video-spend', 0,
    `no video line items in the last ${days} day(s). That is a proxy: it means `
    + 'nothing was billed, not that nothing calls the endpoint'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  const line = REPAIRS[state];
  if (!line) return [];
  if (state === 'shutdown-dated' || state === 'past-shutdown' || state === 'already-gone') {
    return [line,
      'there is no successor model id to print here. The replacement column is '
      + 'empty for every Sora id in the deprecation table.'];
  }
  return [line];
}

async function getJson(path, key, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(one));
  }
  try {
    const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
    let body = {};
    try { body = await r.json(); } catch { body = {}; }
    return [r.status, body];
  } catch {
    return [null, {}];
  }
}

async function allVideos(key, pages = 50) {
  const out = [];
  let after = null;
  for (let i = 0; i < pages; i += 1) {
    const params = { limit: 100, order: 'asc' };
    if (after) params.after = after;
    const [status, body] = await getJson('/videos', key, params);
    if (status !== 200) {
      console.log(`video listing came back ${status}, so the inventory is incomplete`);
      break;
    }
    const page = body.data || [];
    out.push(...page);
    if (!page.length || !body.has_more) break;
    after = page[page.length - 1].id;
    if (!after) break;
  }
  return out;
}

async function costRows(key, days) {
  const start = Math.floor(Date.now() / 1000) - days * 86400;
  const [status, body] = await getJson('/organization/costs', key, {
    start_time: start, bucket_width: '1d', 'group_by[]': ['line_item'], limit: 180,
  });
  if (status !== 200) {
    console.log(`cost report came back ${status}, so the surface was not sized`);
    return [];
  }
  const rows = [];
  for (const bucket of body.data || []) {
    for (const row of bucket.results || []) {
      rows.push([row.line_item, (row.amount || {}).value]);
    }
  }
  return rows;
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project read key. This script only '
                  + 'issues GET requests');
    process.exitCode = 2;
    return;
  }
  const today = (process.env.TODA || "dummy-toda")Y || new Date().toISOString().slice(0, 10);
  const days = Number((process.env.DAY || "dummy-day")S || 30);
  let findings = 0;

  console.log(`endpoint /v1/videos closes ${SHUTDOWN}, ${daysLeft(today)} day(s) left`);
  for (const modelId of SORA_IDS) {
    const [status, body] = await getJson(`/models/${modelId}`, key);
    const [state, detail] = modelVerdict(modelId, status, body.shutdown_date, today);
    console.log(`  ${modelId.padEnd(26)} ${status ?? '---'}  ${state.padEnd(15)} ${detail}`);
    if (replacementFor(modelId)) {
      console.error('  the replacement table is not empty. Read the note before '
                    + 'trusting this line');
    }
  }
  console.log(`  ${'no-replacement'.padEnd(26)} the deprecation table lists no successor `
              + 'for any of these ids, so there is no string to substitute');
  for (const line of repairLines('shutdown-dated')) console.log(`  repair: ${line}`);
  findings += 1;

  const videos = await allVideos(key);
  console.log(`${videos.length} asset(s) in the inventory`);
  const buckets = new Map();
  for (const video of videos) {
    const [state, deadline, detail] = assetDeadline(isoDay(video.expires_at), today);
    const entry = buckets.get(state) || [0, deadline, detail];
    entry[0] += 1;
    if (deadline < entry[1]) { entry[1] = deadline; entry[2] = detail; }
    buckets.set(state, entry);
  }
  for (const [state, [count, deadline, detail]] of
       [...buckets.entries()].sort((a, b) => (a[1][1] < b[1][1] ? -1 : 1))) {
    console.log(`  ${state.padEnd(22)} ${String(count).padStart(4)}  earliest ${deadline}: ${detail}`);
    for (const line of repairLines(state)) console.log(`    repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.log(`${'not-sized'.padEnd(22)} no admin key, so the surface was not sized`);
  } else {
    const [state, , detail] = spendVerdict(await costRows(admin, days), days);
    console.log(`${state.padEnd(22)} ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
