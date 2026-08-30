/**
 * Report an Anthropic output limiter that concurrency cannot fix.
 *
 * Read only. Two GET requests and nothing else against the Admin API, which
 * needs an Admin API key (sk-ant-admin...); a workspace key is rejected by
 * every /v1/organizations/* path. The repair is printed, never performed.
 *
 * The messages usage report has no request-count field, so this script never
 * claims a request rate: it divides the peak output minute by the configured
 * RPM and prints the answer length at which the request limiter would have
 * bound first.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const LIMITER_TYPES = ['requests_per_minute', 'input_tokens_per_minute',
                       'output_tokens_per_minute'];

const FINDINGS = new Set(['otpm-saturated', 'both-limiters-saturated']);

const int = (v) => (Number.isFinite(Number(v)) ? Math.trunc(Number(v)) : 0);

/**
 * Output tokens in one usage result. Pure.
 * Thinking tokens are billed and counted as output, so they are already inside
 * this number: there is nothing to add and nothing to subtract.
 */
export function generated(result) {
  if (!result || typeof result !== 'object') return 0;
  return int(result.output_tokens);
}

/**
 * Input tokens in one usage result, from every field that carries them. Pure.
 * Summed generously rather than charged the way ITPM charges, because this is
 * only used to decide whether the input limiter also had pressure on it.
 */
export function received(result) {
  if (!result || typeof result !== 'object') return 0;
  const creation = result.cache_creation ?? {};
  return int(result.uncached_input_tokens) + int(result.cache_read_input_tokens)
    + int(creation.ephemeral_5m_input_tokens) + int(creation.ephemeral_1h_input_tokens);
}

/**
 * Fold one-minute buckets into per-model output peaks. Pure.
 * The input kept is the input from the minute output peaked, not the largest
 * input minute: two peaks from two minutes describe a workload that never ran.
 */
export function peaks(buckets) {
  const perMinute = new Map();
  for (const bucket of buckets ?? []) {
    const stamp = String(bucket.starting_at ?? bucket.start_time ?? '');
    for (const result of bucket.results ?? []) {
      const model = String(result.model ?? '').trim() || 'all models';
      const key = `${model}\u0000${stamp}`;
      const row = perMinute.get(key) ?? { model, stamp, out: 0, in: 0 };
      row.out += generated(result);
      row.in += received(result);
      perMinute.set(key, row);
    }
  }

  const out = {};
  for (const row of perMinute.values()) {
    const stats = out[row.model] ?? { peak_out: 0, peak_at: null, input_at_peak: 0,
                                      minutes: 0, total_out: 0 };
    stats.minutes += 1;
    stats.total_out += row.out;
    if (row.out > stats.peak_out) {
      stats.peak_out = row.out;
      stats.peak_at = row.stamp;
      stats.input_at_peak = row.in;
    }
    out[row.model] = stats;
  }
  return out;
}

/**
 * {model_group: {limiter type: value}} from the rate-limits response. Pure.
 * All three limiters are kept; a type absent from limits[] is null, which means
 * it inherits, never that it is unlimited.
 */
export function limitsByGroup(payload) {
  const out = {};
  for (const entry of (payload ?? {}).data ?? []) {
    const group = String(entry.model_group ?? '').trim();
    if (!group) continue;
    if (!out[group]) {
      out[group] = {};
      for (const t of LIMITER_TYPES) out[group][t] = null;
    }
    for (const limit of entry.limits ?? []) {
      const kind = String(limit.type ?? '').trim();
      if (!(kind in out[group])) continue;
      const value = Number(limit.value);
      out[group][kind] = Number.isInteger(value) ? value : null;
    }
  }
  return out;
}

/** The limiter row for the group a model id belongs to. Pure. Longest prefix wins. */
export function limitsFor(groups, model) {
  const name = String(model ?? '').trim().toLowerCase();
  if (!name) return null;
  let bestKey = null;
  let bestLen = -1;
  for (const group of Object.keys(groups ?? {})) {
    const candidate = group.trim().toLowerCase();
    if (!candidate) continue;
    if (name === candidate || name.startsWith(candidate)) {
      if (candidate.length > bestLen) { bestKey = group; bestLen = candidate.length; }
    }
  }
  return bestKey === null ? null : groups[bestKey];
}

/**
 * Answer length at which RPM would bind before OTPM. Pure.
 * peak_output / rpm. Longer answers than that and the request rate was never
 * close. This exists because the usage report has no request count: it turns a
 * question the API cannot answer into one the reader already knows.
 */
export function impliedMeanOutput(peakOutput, rpm) {
  if (rpm === null || rpm === undefined || rpm <= 0) return null;
  const peak = Number(peakOutput ?? 0);
  if (!Number.isFinite(peak) || peak <= 0) return null;
  return peak / rpm;
}

/**
 * OTPM as a share of ITPM for one model group. Pure.
 * Roughly one fifth at every tier, which is why generation hits its ceiling
 * first. Printed rather than assumed, because an override can change it.
 */
export function outputToInputRatio(limits) {
  if (!limits || typeof limits !== 'object') return null;
  const otpm = limits.output_tokens_per_minute;
  const itpm = limits.input_tokens_per_minute;
  if (otpm === null || otpm === undefined) return null;
  if (itpm === null || itpm === undefined || itpm <= 0) return null;
  return otpm / itpm;
}

/** Classify one model's output limiter. Pure. Returns [state, detail]. */
export function verdict(model, stats, limits, {
  floor = 0.9, watch = 0.6, minMinutes = 10,
} = {}) {
  const minutes = Number((stats ?? {}).minutes ?? 0);
  if (minutes < minMinutes) {
    return ['too-few-buckets',
      `${minutes} minute(s) of traffic in the window, under the floor of ` +
      `${minMinutes}. A peak taken over this little is noise.`];
  }

  const row = (limits && typeof limits === 'object') ? limits : {};
  const otpm = row.output_tokens_per_minute;
  if (otpm === null || otpm === undefined || otpm <= 0) {
    return ['no-limit-published',
      "no output_tokens_per_minute is published for this model's group, so " +
      'there is no ceiling to compare the peak against. The limiter still ' +
      'exists; the number was simply not returned.'];
  }

  const peakOut = Number(stats.peak_out ?? 0);
  const outUsed = peakOut / otpm;

  const itpm = row.input_tokens_per_minute;
  let inUsed = null;
  if (itpm !== null && itpm !== undefined && itpm > 0) {
    inUsed = Number(stats.input_at_peak ?? 0) / itpm;
  }

  let shape = `peak minute generated ${peakOut} of an OTPM of ${otpm} ` +
              `(${(outUsed * 100).toFixed(0)}%)`;
  shape += inUsed === null
    ? ', with no ITPM published to compare'
    : ` while input sat at ${(inUsed * 100).toFixed(0)}% of ITPM`;

  if (outUsed >= floor && inUsed !== null && inUsed >= floor) {
    return ['both-limiters-saturated',
      `${shape}. Both token limiters are full, so this is volume rather than ` +
      'shape: caching the prefix helps the input side and does nothing for the ' +
      'output side, and only batching or a limit increase moves both.'];
  }
  if (outUsed >= floor) {
    return ['otpm-saturated',
      `${shape}. The output limiter is what you are hitting, and there is no ` +
      'cached output, so nothing about the prompt moves this number.'];
  }
  if (inUsed !== null && inUsed >= floor && outUsed < watch) {
    return ['input-bound',
      `${shape}. The input limiter is the one that is full here, not the output ` +
      'one. Cache reads are not charged against ITPM, so that is a different ' +
      'finding with a different repair.'];
  }
  if (outUsed >= watch) {
    return ['otpm-approaching',
      `${shape}. Thin enough that a rise in answer length, or in thinking ` +
      'effort, lands on the output limiter.'];
  }
  return ['otpm-headroom', `${shape}.`];
}

/** Floor to the minute: starting_at must sit on a bucket boundary. */
export function windowStart(minutes, now = new Date()) {
  const floored = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
                           now.getUTCHours(), now.getUTCMinutes());
  return new Date(floored - minutes * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

async function get(adminKey, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, one);
  }
  const res = await fetch(url, {
    headers: { 'x-api-key': adminKey, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: /v1/organizations/* needs an ` +
                    'Admin API key (sk-ant-admin...), not a workspace key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

async function* readBuckets(adminKey, path, params) {
  const q = { ...params };
  for (;;) {
    const page = await get(adminKey, path, q);
    for (const bucket of page.data ?? []) yield bucket;
    if (!page.has_more || !page.next_page) return;
    q.page = page.next_page;
  }
}

async function main() {
  const adminKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!adminKey) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); ' +
                  'a workspace key cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const minutes = Math.max(1, Math.min(Number((process.env.MINUTES || "dummy-minutes") ?? 240), 1440));
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const collected = [];
  for await (const bucket of readBuckets(adminKey, '/organizations/usage_report/messages',
    { starting_at: windowStart(minutes), bucket_width: '1m', limit: minutes,
      'group_by[]': ['model'] })) {
    collected.push(bucket);
  }
  const stats = peaks(collected);
  const models = Object.keys(stats);
  if (models.length === 0) {
    console.log(`no message usage in the last ${minutes} minute(s)`);
    return;
  }

  const groups = limitsByGroup(await get(adminKey, '/organizations/rate_limits'));

  let bad = 0;
  models.sort((a, b) => stats[b].peak_out - stats[a].peak_out);
  for (const model of models) {
    const row = stats[model];
    const limits = limitsFor(groups, model);
    const [state, detail] = verdict(model, row, limits);
    const line = `${state.padEnd(24)} ${model.padEnd(28)} ${detail}`;

    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      const mean = impliedMeanOutput(row.peak_out, (limits ?? {}).requests_per_minute);
      if (mean !== null) {
        console.warn(`  RPM would only have bound first at a mean answer of ` +
                     `${mean.toFixed(0)} token(s) or shorter, so if your answers are ` +
                     'longer than that the request rate was never the ceiling and ' +
                     'more workers add nothing');
      } else {
        console.warn('  no requests_per_minute published for this group, so the ' +
                     'request rate cannot be ruled out from here');
      }
      const ratio = outputToInputRatio(limits);
      if (ratio !== null) {
        console.warn(`  OTPM is ${(ratio * 100).toFixed(0)}% of ITPM on this group, ` +
                     'so generation reaches its ceiling first');
      }
      console.warn('  repair: move latency tolerant generation to the Message ' +
                   'Batches API, which has its own limiter group and costs half; or ' +
                   'lower output_config.effort, since thinking tokens are counted as ' +
                   'output; or request an output_tokens_per_minute increase.');
      console.warn('  repair: do not lower max_tokens. It is documented not to factor ' +
                   'into OTPM, so it truncates answers without buying a single token ' +
                   'of headroom.');
    } else if (state === 'input-bound') {
      console.warn(line);
      console.warn('  repair: this one is the input limiter. Cache reads are not ' +
                   'charged against ITPM, so covering the stable prefix is the lever ' +
                   'there, not anything on this page.');
    } else if (state === 'otpm-approaching' || state === 'no-limit-published') {
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${models.length} model(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
