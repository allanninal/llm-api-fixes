/**
 * Find OpenAI projects whose usage buckets went empty while the project is live.
 *
 * Read only. GET requests against the organization endpoints, which reject
 * project keys: this needs an organization admin key (sk-admin-).
 *
 * The finding is an absence, so the day axis is built from the window that was
 * requested rather than from the days that came back. The repair is printed,
 * never performed: what is missing is an alarm with a floor instead of a
 * ceiling, and that lives in your monitoring.
 */
const API = 'https://api.openai.com/v1';

const SURFACES = ['completions', 'embeddings', 'images', 'audio_speeches',
                  'audio_transcriptions', 'moderations', 'file_search_calls',
                  'web_search_calls'];

const COUNT_FIELDS = ['num_model_requests', 'num_requests', 'num_images',
                      'num_seconds', 'num_characters'];

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** The UTC day a bucket start belongs to. Pure. Null if unreadable. */
export function dayKey(epoch) {
  const n = Number(epoch);
  if (!Number.isFinite(n)) return null;
  const when = new Date(Math.trunc(n) * 1000);
  if (Number.isNaN(when.getTime())) return null;
  return when.toISOString().slice(0, 10);
}

/**
 * The last N complete UTC days, oldest first. Pure.
 * Today is excluded: the current bucket is partial and usage data lags, so a
 * run of zeroes that includes it is one day shorter than it looks.
 */
export function completeDays(nowEpoch, days) {
  const out = [];
  for (let offset = Math.trunc(days); offset > 0; offset -= 1) {
    const key = dayKey(Math.trunc(nowEpoch) - offset * 86400);
    if (key !== null) out.push(key);
  }
  return out;
}

/**
 * {project_id: {day: count}} from one usage surface. Pure.
 * First recognised count field wins rather than being summed.
 */
export function daily(buckets) {
  const out = new Map();
  for (const bucket of buckets ?? []) {
    const day = dayKey(bucket?.start_time);
    if (day === null) continue;
    for (const result of bucket?.results ?? []) {
      const project = String(result?.project_id ?? 'unknown');
      let count = 0;
      for (const field of COUNT_FIELDS) {
        if (result && field in result) { count = readInt(result[field]); break; }
      }
      if (!out.has(project)) out.set(project, new Map());
      const row = out.get(project);
      row.set(day, (row.get(day) ?? 0) + count);
    }
  }
  return out;
}

/**
 * Classify one project's daily series. Pure. Returns [state, detail].
 * Directional on purpose: traffic early and none late is a project that
 * stopped, the reverse is one that started, and a check that cannot tell them
 * apart fires on every launch and gets muted.
 */
export function classify(series, days, quietDays = 2, minRequests = 100) {
  const axis = [...(days ?? [])];
  if (axis.length <= quietDays) {
    return ['window-too-short',
      `${axis.length} complete day(s) is not enough to hold a ${quietDays} ` +
      'day quiet window'];
  }

  const at = (day) => readInt(series?.get ? series.get(day) : series?.[day]);
  const head = axis.slice(0, axis.length - quietDays);
  const tail = axis.slice(axis.length - quietDays);
  const prior = head.reduce((sum, day) => sum + at(day), 0);
  const recent = tail.reduce((sum, day) => sum + at(day), 0);
  const active = axis.filter((day) => at(day) > 0);

  if (active.length === 0) {
    return ['never-active', `no traffic at all across ${axis.length} complete day(s)`];
  }
  if (prior === 0) {
    return ['new-traffic',
      `first traffic in this window landed on ${active[0]}, inside the last ` +
      `${quietDays} day(s). A launch reads exactly like a death if you only ` +
      'compare halves.'];
  }
  if (recent > 0) {
    return ['live',
      `${recent} request(s) in the last ${quietDays} day(s), against a prior ` +
      `mean of ${Math.trunc(prior / head.length)} a day`];
  }
  if (prior < minRequests) {
    return ['too-little-traffic',
      `${prior} request(s) before the quiet window, under the floor of ` +
      `${minRequests}. Too sporadic for a gap to mean anything.`];
  }

  const last = active[active.length - 1];
  const since = axis.length - 1 - axis.indexOf(last);
  return ['went-quiet',
    `last traffic on ${last}, ${since} complete day(s) ago, after a prior mean ` +
    `of ${Math.trunc(prior / head.length)} request(s) a day`];
}

/** The newest last_used_at across a project's keys. Pure. [epoch, daysSince]. */
export function keyActivity(keys, nowEpoch) {
  let best = null;
  for (const key of keys ?? []) {
    const used = key?.last_used_at;
    if (used === null || used === undefined) continue;
    const n = Number(used);
    if (!Number.isFinite(n)) continue;
    if (best === null || n > best) best = Math.trunc(n);
  }
  if (best === null) return [null, null];
  return [best, Math.max(0, (Math.trunc(nowEpoch) - best) / 86400)];
}

/**
 * Line the key roster up against the silence. Pure. Returns [state, detail].
 * A key still in use while the buckets are empty is a much narrower fault.
 */
export function corroborate(daysSince, quietDays = 2) {
  if (daysSince === null || daysSince === undefined) {
    return ['no-key-use',
      'no key on this project reports a last use, so there is nothing here to ' +
      'corroborate the silence with'];
  }
  if (daysSince <= quietDays) {
    return ['key-still-used',
      `a key on this project was used ${daysSince.toFixed(1)} day(s) ago while ` +
      'the usage buckets were empty. Something is still authenticating and not ' +
      'inferring: a health check, or a surface this sweep did not read.'];
  }
  return ['key-quiet-too',
    `the newest key use is ${daysSince.toFixed(1)} day(s) ago, which lines up ` +
    'with the buckets. The integration went quiet, not one call site.'];
}

/** [quiet, live] surface names for one project. Pure. */
export function surfaceSplit(states) {
  const entries = states instanceof Map ? [...states] : Object.entries(states ?? {});
  const quiet = entries.filter(([, s]) => s === 'went-quiet').map(([n]) => n).sort();
  const live = entries.filter(([, s]) => s === 'live').map(([n]) => n).sort();
  return [quiet, live];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* pages(key, path, params, maxPages = 40) {
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, path, query);
    for (const bucket of page?.data ?? []) yield bucket;
    if (!page?.has_more || !page?.next_page) return;
    query = { ...params, page: page.next_page };
  }
}

async function* listing(key, path, params, maxPages = 20) {
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, path, query);
    const data = page?.data ?? [];
    for (const item of data) yield item;
    if (!page?.has_more || data.length === 0) return;
    query = { ...params, after: data[data.length - 1]?.id };
  }
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key; read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const quietDays = Number((process.env.QUIET_DAYS || "dummy-quiet-days") ?? 2);
  const minRequests = Number((process.env.MIN_REQUESTS || "dummy-min-requests") ?? 100);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';
  const days = completeDays(now, Math.max(3, Math.min(Number((process.env.DAYS || "dummy-days") ?? 14), 30)));

  const projects = [];
  for await (const project of listing(admin, '/organization/projects', { limit: 100 })) {
    if (String(project?.status ?? '') === 'active') projects.push(project);
  }
  if (projects.length === 0) {
    console.log('no active projects in this organization');
    return;
  }

  const perSurface = new Map();
  for (const surface of SURFACES) {
    try {
      const buckets = [];
      for await (const bucket of pages(admin, `/organization/usage/${surface}`, {
        start_time: now - (days.length + 1) * 86400,
        bucket_width: '1d',
        limit: days.length + 1,
        group_by: ['project_id'],
      })) buckets.push(bucket);
      perSurface.set(surface, daily(buckets));
    } catch {
      console.log(`skipped the ${surface} usage surface`);
    }
  }

  let checked = 0;
  let bad = 0;
  for (const project of projects) {
    const projectId = String(project?.id ?? '');
    const states = new Map();
    const details = new Map();
    for (const [surface, rows] of perSurface) {
      const [state, detail] = classify(rows.get(projectId), days, quietDays, minRequests);
      states.set(surface, state);
      details.set(surface, detail);
    }
    checked += 1;

    const [quiet, live] = surfaceSplit(states);
    if (quiet.length === 0) {
      if (showAll) console.log(`live               ${projectId}  no surface went quiet`);
      continue;
    }

    bad += 1;
    console.warn(`went-quiet         ${projectId}  ${quiet[0]}: ${details.get(quiet[0])}`);
    const keys = [];
    for await (const key of listing(admin, `/organization/projects/${projectId}/api_keys`,
                                    { limit: 100, owner_project_access: 'any' })) {
      keys.push(key);
    }
    const [, note] = corroborate(keyActivity(keys, now)[1], quietDays);
    console.warn(`  ${note}`);
    if (live.length > 0) {
      console.warn(`  still live on: ${live.join(', ')}`);
      console.warn('  repair: one code path stopped calling, not the credential. ' +
                   'Look at the deploy that touched it rather than at the key.');
    } else {
      console.warn('  repair: every surface is quiet, so look at the credential, ' +
                   'the feature flag or the consumer before the call site.');
    }
    console.warn('  repair: add a scheduled liveness check that alerts on absence. ' +
                 'Read /v1/organization/usage/completions daily with ' +
                 'group_by=project_id and page on next_page, and alert when a ' +
                 'project falls below a floor rather than above a ceiling. This is ' +
                 'the one check whose value is that it fires on zero.');
  }

  console.log(`${checked} active project(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
