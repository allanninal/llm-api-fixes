/**
 * Report OpenAI request volume growing faster than the tokens it carries.
 *
 * Read only. One GET against the organization usage report, plus one per
 * finding against the project rate limits. Both need an organization admin key
 * (sk-admin-), which can be provisioned read-only.
 *
 * Both series come off the provider's own report, so no telemetry of your own
 * is required. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

const FINDINGS = new Set(['retry-storm', 'requests-outpacing-tokens']);

/** Read a usage field as an integer. Pure. Missing and unreadable both mean 0. */
export function readInt(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/**
 * Per (project, model), the hourly points. Pure.
 * Buckets are kept rather than totalled: the method is a comparison between
 * two halves of the window, and a sum has already thrown the halves away.
 */
export function series(buckets) {
  const out = new Map();
  for (const bucket of buckets ?? []) {
    const start = readInt(bucket?.start_time);
    for (const result of bucket?.results ?? []) {
      const key = `${result?.project_id ?? 'unknown'}\u0000${result?.model ?? 'unknown'}`;
      if (!out.has(key)) {
        out.set(key, { project: String(result?.project_id ?? 'unknown'),
                       model: String(result?.model ?? 'unknown'), points: [] });
      }
      out.get(key).points.push({
        start,
        requests: readInt(result?.num_model_requests),
        tokens: readInt(result?.input_tokens) + readInt(result?.output_tokens),
      });
    }
  }
  for (const row of out.values()) row.points.sort((a, b) => a.start - b.start);
  return out;
}

/**
 * Sum one series into [prior, recent] either side of a cutoff. Pure.
 * Points at or after partialAfter are dropped: the hour the clock is still
 * inside is always short, and leaving it in reports a decline every run.
 */
export function foldWindows(points, cutoff, partialAfter = null) {
  const prior = { requests: 0, tokens: 0, buckets: 0 };
  const recent = { requests: 0, tokens: 0, buckets: 0 };
  for (const point of points ?? []) {
    const start = readInt(point?.start);
    if (partialAfter !== null && partialAfter !== undefined && start >= partialAfter) continue;
    const window = start >= cutoff ? recent : prior;
    window.requests += readInt(point?.requests);
    window.tokens += readInt(point?.tokens);
    window.buckets += 1;
  }
  return [prior, recent];
}

/**
 * recent / prior, or null when there is nothing to divide by. Pure.
 * Null rather than Infinity: a workload that did not exist last week has no
 * growth rate, and a huge number would put every new deployment on top.
 */
export function growth(priorValue, recentValue) {
  const prior = Number(priorValue ?? 0);
  if (!(prior > 0)) return null;
  return Number(recentValue ?? 0) / prior;
}

/** Mean tokens per request in one window, or null. Pure. */
export function tokensPerRequest(window) {
  const made = readInt(window?.requests);
  if (made <= 0) return null;
  return readInt(window?.tokens) / made;
}

/**
 * Request growth divided by token growth. Pure. Null when unavailable.
 * This number is exactly the reciprocal of the change in tokens per request.
 * The first version of this script tested both and read them as two agreeing
 * witnesses; they are one witness stated twice, which is why burstiness exists.
 */
export function divergenceRatio(prior, recent) {
  const requestGrowth = growth(readInt(prior?.requests), readInt(recent?.requests));
  const tokenGrowth = growth(readInt(prior?.tokens), readInt(recent?.tokens));
  if (requestGrowth === null || tokenGrowth === null || tokenGrowth <= 0) return null;
  return requestGrowth / tokenGrowth;
}

/**
 * Share of the recent window's requests in its busiest hours. Pure.
 * Evenly spread traffic puts about topShare of its requests there; a retry
 * storm puts most of them there, because retries amplify during the failures
 * that caused them. Null when there are too few hours to mean anything.
 */
export function burstiness(points, cutoff, partialAfter = null, topShare = 0.1,
                           minBuckets = 24) {
  const recent = [];
  for (const point of points ?? []) {
    const start = readInt(point?.start);
    if (partialAfter !== null && partialAfter !== undefined && start >= partialAfter) continue;
    if (start >= cutoff) recent.push(readInt(point?.requests));
  }
  if (recent.length < minBuckets) return null;
  const total = recent.reduce((a, b) => a + b, 0);
  if (total <= 0) return null;
  const top = Math.max(1, Math.round(recent.length * topShare));
  const head = [...recent].sort((a, b) => b - a).slice(0, top);
  return head.reduce((a, b) => a + b, 0) / total;
}

/**
 * Compare two windows of one series. Pure. Returns [state, detail].
 * The divergence says the request count grew on its own; the burst share says
 * whether it grew in the shape retries have.
 */
export function classify(prior, recent, burst = null, divergence = 2.0,
                         minRequests = 1000, burstFloor = 0.35) {
  const priorRequests = readInt(prior?.requests);
  const recentRequests = readInt(recent?.requests);

  if (priorRequests < minRequests && recentRequests < minRequests) {
    return ['too-little-traffic',
      `${priorRequests} request(s) then ${recentRequests}, both under the ` +
      `floor of ${minRequests}`];
  }

  const requestGrowth = growth(priorRequests, recentRequests);
  const tokenGrowth = growth(readInt(prior?.tokens), readInt(recent?.tokens));
  if (requestGrowth === null || tokenGrowth === null) {
    return ['new-workload',
      `nothing in the prior window to compare against: ${recentRequests} ` +
      `request(s) and ${readInt(recent?.tokens)} token(s) appeared this week`];
  }

  const before = tokensPerRequest(prior) ?? 0;
  const after = tokensPerRequest(recent) ?? 0;
  let shape = `requests x${requestGrowth.toFixed(2)}, tokens x` +
    `${tokenGrowth.toFixed(2)}, tokens per request ${Math.trunc(before)} then ` +
    `${Math.trunc(after)}`;
  if (burst !== null && burst !== undefined) {
    shape += `; ${(burst * 100).toFixed(0)}% of the surplus landed in the ` +
      'busiest 10% of hours';
  }

  if (requestGrowth >= divergence * tokenGrowth) {
    if (burst === null || burst === undefined) {
      return ['retry-storm',
        `${shape}. Too few hourly buckets to measure how concentrated the ` +
        'surplus was, so this rests on the growth ratio alone.'];
    }
    if (burst < burstFloor) {
      return ['requests-outpacing-tokens',
        `${shape}. The extra calls are spread evenly across the hours rather ` +
        'than piled into a few, which is a workload that got shorter rather ' +
        'than one being retried.'];
    }
    return ['retry-storm',
      `${shape}. The surplus arrived in bursts, which is what retries do: they ` +
      'amplify during the failures that caused them.'];
  }

  if (tokenGrowth >= divergence * requestGrowth) {
    return ['prompts-grew',
      `${shape}. Tokens moved and the call count did not, so this is prompt ` +
      'or answer length, not call volume.'];
  }

  if (requestGrowth >= 1.25 && tokenGrowth >= 1.25) {
    return ['traffic-growth',
      `${shape}. Both series moved together, which is traffic rather than ` +
      'amplification.'];
  }

  if (requestGrowth <= 0.75) {
    return ['quieter', `${shape}. Fewer calls than the week before.`];
  }

  return ['steady', `${shape}.`];
}

/** The RPM and TPM this project publishes for a model. Pure. Longest prefix wins. */
export function rateLimitValues(payload, model) {
  const name = String(model ?? '').trim().toLowerCase();
  let best = null;
  let bestLen = -1;
  for (const entry of payload?.data ?? []) {
    const candidate = String(entry?.model ?? '').trim().toLowerCase();
    if (!candidate) continue;
    if ((name === candidate || name.startsWith(candidate)) && candidate.length > bestLen) {
      best = entry;
      bestLen = candidate.length;
    }
  }
  if (best === null) return { requests: null, tokens: null };
  const read = (field) => {
    const n = Number(best[field]);
    return Number.isFinite(n) ? Math.trunc(n) : null;
  };
  return { requests: read('max_requests_per_1_minute'),
           tokens: read('max_tokens_per_1_minute') };
}

/**
 * Where a window's mean traffic sits against RPM and TPM. Pure.
 * An hourly mean spread across sixty minutes is a floor on the real peak and
 * never the peak itself, which is all this needs to be.
 */
export function limiterPressure(window, hours, limits, near = 0.7, idle = 0.3) {
  const rpmLimit = limits?.requests;
  const tpmLimit = limits?.tokens;
  const minutes = Math.max(1, readInt(hours) * 60);
  if (!rpmLimit && !tpmLimit) {
    return ['no-limits-published',
      'this project publishes no rate limit for the model, so there is no ' +
      'ceiling to compare the mean against'];
  }

  const rpmUsed = rpmLimit ? readInt(window?.requests) / minutes / rpmLimit : null;
  const tpmUsed = tpmLimit ? readInt(window?.tokens) / minutes / tpmLimit : null;
  const pct = (v) => (v === null ? 'an unpublished share' : `${(v * 100).toFixed(0)}%`);
  const shape = `hourly mean sits at ${pct(rpmUsed)} of the RPM ceiling and ` +
    `${pct(tpmUsed)} of the TPM ceiling`;

  if (rpmUsed !== null && tpmUsed !== null) {
    if (rpmUsed >= near && tpmUsed <= idle) {
      return ['rpm-bound-tpm-idle',
        `${shape}, which is what amplification looks like from the limiter ` +
        'side: the request bucket fills and the token bucket does not'];
    }
    if (rpmUsed >= near && tpmUsed >= near) {
      return ['both-near', `${shape}, so both limiters are under pressure`];
    }
    if (tpmUsed >= near) {
      return ['tpm-bound',
        `${shape}, so the token limiter is the binding one and this is volume ` +
        'rather than retries'];
    }
  }
  return ['headroom', shape];
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

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key; read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(2, Math.min(Number((process.env.DAYS || "dummy-days") ?? 14), 30));
  const half = Math.floor(days / 2);
  const now = Math.floor(Date.now() / 1000);
  const cutoff = now - half * 86400;
  const partialAfter = now - (now % 3600);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const buckets = [];
  for await (const bucket of pages(admin, '/organization/usage/completions', {
    start_time: now - days * 86400,
    bucket_width: '1h',
    limit: 168,
    group_by: ['model', 'project_id'],
  })) buckets.push(bucket);

  const rows = series(buckets);
  if (rows.size === 0) {
    console.log(`no completions usage in the last ${days} day(s)`);
    return;
  }

  let checked = 0;
  let bad = 0;
  for (const row of [...rows.values()].sort((a, b) =>
    `${a.project}${a.model}`.localeCompare(`${b.project}${b.model}`))) {
    const [prior, recent] = foldWindows(row.points, cutoff, partialAfter);
    const burst = burstiness(row.points, cutoff, partialAfter);
    const [state, detail] = classify(prior, recent, burst);
    checked += 1;
    const line = `${state.padEnd(26)} ${row.project} / ${row.model}  ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      let limits = { requests: null, tokens: null };
      if (row.project !== 'unknown') {
        try {
          limits = rateLimitValues(
            await get(admin, `/organization/projects/${row.project}/rate_limits`,
                      { limit: 100 }), row.model);
        } catch {
          limits = { requests: null, tokens: null };
        }
      }
      const [, pressure] = limiterPressure(recent, half * 24, limits);
      console.warn(`  ${pressure}`);
      if (state === 'retry-storm') {
        console.warn('  repair: collapse to one retry layer. Set max_retries ' +
                     'explicitly on the SDK client and remove the outer wrapper, ' +
                     'or set it to 0 and keep the wrapper. Exponential backoff ' +
                     'with jitter, and a circuit breaker so a sustained failure ' +
                     'stops re-amplifying.');
        console.warn('  repair: raising the project rate limit is the second ' +
                     'measure, not the first. An admin can call POST ' +
                     '/v1/organization/projects/{project_id}/rate_limits/' +
                     '{rate_limit_id} once the layering is fixed. It is printed ' +
                     'here, not run.');
      } else {
        console.warn('  repair: nothing yet. Confirm the shorter calls are a ' +
                     'real workload before changing any retry policy, and ' +
                     're-run next week.');
      }
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${checked} model/project series checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
