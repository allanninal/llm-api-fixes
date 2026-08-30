/**
 * Find 429s caused by the ramp rather than by the limit.
 *
 * Read only. Two GETs with an Admin API key: the messages usage report at
 * minute granularity grouped by model, and the configured rate limits.
 *
 * The finding is a shape between two adjacent minutes, not a level in one: a
 * steep step whose peak never approaches the ceiling. Saturation is graded
 * first and handed to the ITPM and OTPM notes, because a ramp reported next to
 * a saturated limiter is a coincidence dressed up as a cause.
 *
 * Input is summed the way the limiter counts it. This report has no request
 * count of any kind, so the ramp is measured in tokens and reported as such.
 */
const API = 'https://api.anthropic.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

export const START_TIER = {
  'claude-fable-5': [500000, 100000],
  'claude-haiku-3-5': [100000, 20000],
};
const START_TIER_DEFAULT = [2000000, 400000];
const COUNTS_CACHE_READS = new Set(['claude-haiku-3-5']);

const SATURATED = 0.85;
const QUIET = 0.60;
const FINDINGS = new Set(['acceleration-suspect', 'ramp-near-ceiling',
                          'below-published-start']);

/** A number, or 0. Pure. */
export function num(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** Both cache creation figures, summed. Pure. */
export function cacheCreation(result) {
  const block = result?.cache_creation ?? {};
  return num(block.ephemeral_5m_input_tokens) + num(block.ephemeral_1h_input_tokens);
}

/** The input tokens that count toward ITPM. Pure. Cache reads excluded. */
export function uncachedInput(result) {
  return num(result?.uncached_input_tokens) + cacheCreation(result);
}

/** {model: [[startingAt, input, output, cacheRead]]}. Pure, ordered. */
export function series(pages, modelKey = 'model') {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      const start = String(bucket?.starting_at ?? '');
      for (const result of bucket?.results ?? []) {
        const model = String(result?.[modelKey] ?? '(ungrouped)');
        (out[model] ??= []).push([start, uncachedInput(result),
                                  num(result?.output_tokens),
                                  num(result?.cache_read_input_tokens)]);
      }
    }
  }
  for (const rows of Object.values(out)) rows.sort((a, b) => a[0].localeCompare(b[0]));
  return out;
}

/** [startingAt, value] for the largest bucket. Pure. */
export function peak(rows, index) {
  let best = ['', 0];
  for (const row of rows ?? []) if (row[index] > best[1]) best = [row[0], row[index]];
  return best;
}

/** value / limit, or null when the limit is unknown. Pure. */
export function share(value, limit) {
  if (!limit || limit <= 0) return null;
  return value / limit;
}

/** [[prevStart, start, factor, prev, current]], largest factor first. Pure. */
export function rampFactors(rows, index, minBase = 10000) {
  const out = [];
  const list = rows ?? [];
  for (let i = 1; i < list.length; i += 1) {
    const prev = list[i - 1][index];
    const current = list[i][index];
    if (prev < minBase || current <= prev) continue;
    out.push([list[i - 1][0], list[i][0], current / prev, prev, current]);
  }
  out.sort((a, b) => (b[2] - a[2]) || a[1].localeCompare(b[1]));
  return out;
}

/** {limiterType: value} for the group containing this model. Pure. Exact match. */
export function groupForModel(groups, model) {
  for (const entry of groups ?? []) {
    const models = (entry?.models ?? []).map(String);
    if (models.includes(String(model))) {
      const out = {};
      for (const row of entry?.limits ?? []) {
        const ltype = String(row?.type ?? '');
        if (ltype) out[ltype] = num(row?.value);
      }
      return out;
    }
  }
  return {};
}

/** [[limiter, configured, publishedStart]] below the documented floor. Pure. */
export function belowPublishedStart(model, limits) {
  const [itpmFloor, otpmFloor] = START_TIER[String(model)] ?? START_TIER_DEFAULT;
  const out = [];
  for (const [ltype, floor] of [['input_tokens_per_minute', itpmFloor],
                                ['output_tokens_per_minute', otpmFloor]]) {
    const configured = limits?.[ltype];
    if (configured && configured > 0 && configured < floor) {
      out.push([ltype, configured, floor]);
    }
  }
  return out;
}

/** Thousands separators. Pure. */
export function fmt(value) {
  return Math.round(num(value)).toLocaleString('en-US');
}

/** Classify one model's window. Pure. Returns [state, detail, facts]. */
export function verdict(rows, limits, model, rampThreshold = 3.0) {
  const list = rows ?? [];
  const lim = limits ?? {};
  const facts = {
    peakIn: peak(list, 1),
    peakOut: peak(list, 2),
    itpm: lim.input_tokens_per_minute,
    otpm: lim.output_tokens_per_minute,
    ramps: [...rampFactors(list, 1), ...rampFactors(list, 2)].sort((a, b) => b[2] - a[2]),
    cacheReadCounts: COUNTS_CACHE_READS.has(String(model)),
  };
  facts.inShare = share(facts.peakIn[1], facts.itpm);
  facts.outShare = share(facts.peakOut[1], facts.otpm);

  if (list.length === 0 || (facts.peakIn[1] <= 0 && facts.peakOut[1] <= 0)) {
    return ['no-traffic', 'no usage in this window', facts];
  }
  if (facts.inShare !== null && facts.inShare >= SATURATED) {
    return ['limiter-saturated',
            `input peaked at ${fmt(facts.peakIn[1])}/min, `
            + `${Math.round(facts.inShare * 100)}% of ITPM. That is the input `
            + 'limiter note, not this one.', facts];
  }
  if (facts.outShare !== null && facts.outShare >= SATURATED) {
    return ['limiter-saturated',
            `output peaked at ${fmt(facts.peakOut[1])}/min, `
            + `${Math.round(facts.outShare * 100)}% of OTPM. That is the output `
            + 'limiter note, not this one.', facts];
  }

  const steepest = facts.ramps.length ? facts.ramps[0][2] : 0;
  if (steepest < rampThreshold) {
    return ['steady', `no adjacent minute rose by ${rampThreshold.toFixed(1)}x or more`,
            facts];
  }
  const shares = [facts.inShare, facts.outShare].filter((s) => s !== null);
  if (shares.length && Math.max(...shares) <= QUIET) {
    return ['acceleration-suspect',
            `a ${steepest.toFixed(1)}x step between adjacent minutes with every `
            + `peak under ${Math.round(QUIET * 100)}% of its ceiling`, facts];
  }
  return ['ramp-near-ceiling',
          `a ${steepest.toFixed(1)}x step between adjacent minutes, and the peak is `
          + `already past ${Math.round(QUIET * 100)}% of a ceiling. Pace it and ask `
          + 'for the increase.', facts];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, facts) {
  if (state === 'acceleration-suspect') {
    const lines = ['ramp gradually and keep usage patterns consistent. A step this '
      + 'steep can 429 on acceleration alone, well under the tier limits, and a '
      + 'limit increase does not change it.',
      'spread the burst across the minute with client-side pacing or a queue in '
      + 'front of the fan-out. A limit of 60 per minute may be enforced as 1 per '
      + 'second, so the shape inside the minute matters.'];
    if (facts?.cacheReadCounts) {
      lines.push('this model counts cache reads toward the input limiter, unlike '
        + 'the others. Add cache_read_input_tokens back before comparing its peak '
        + 'against ITPM.');
    }
    return lines;
  }
  if (state === 'ramp-near-ceiling') {
    return ['pace the ramp and request the increase. Both are true here: the step '
      + 'is steep enough to trip acceleration and the peak is close enough that a '
      + 'bigger ceiling would also help.'];
  }
  if (state === 'limiter-saturated') {
    return ['this one really is the headline number. Read the input or output '
      + 'limiter note for the reading that fits, rather than pacing traffic that '
      + 'is genuinely at its ceiling.'];
  }
  if (state === 'below-published-start') {
    return ['configured limits below the published Start tier usually mean an '
      + 'Evaluation tier organization, where the documentation tables do not '
      + 'apply. Stop reasoning from the tables and read /v1/organizations/'
      + 'rate_limits instead.',
      'Evaluation limits rise automatically as the organization builds usage '
      + 'history, so this is a reason to pace traffic rather than a reason to '
      + 'file anything.'];
  }
  return [];
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) url.searchParams.append(k, String(v));
  const r = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION,
               'User-Agent': 'anthropic-ramp-acceleration/1.0' },
  });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from Anthropic: the usage report and the rate `
                    + 'limits endpoint need an Admin API credential');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function pages(key, path, params) {
  const out = [];
  const q = { ...(params ?? {}) };
  for (let i = 0; i < 50; i += 1) {
    const page = await read(key, path, q);
    out.push(page);
    if (!page.next_page) break;
    q.page = page.next_page;
  }
  return out;
}

async function main() {
  const key = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!key) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key or another '
                  + 'organization scoped read credential');
    process.exitCode = 2;
    return;
  }
  const hours = Math.max(0.1, Math.min(24, Number((process.env.HOURS || "dummy-hours") ?? 4)));
  const rampThreshold = Number((process.env.RAMP || "dummy-ramp") ?? 3);

  const now = new Date();
  now.setUTCSeconds(0, 0);
  const start = new Date(now.getTime() - hours * 3600 * 1000);
  const stamp = (d) => `${d.toISOString().slice(0, 19)}Z`;

  const buckets = await pages(key, '/organizations/usage_report/messages', {
    starting_at: stamp(start), ending_at: stamp(now),
    bucket_width: '1m', limit: 1440, 'group_by[]': 'model',
  });
  const groups = (await pages(key, '/organizations/rate_limits'))
    .flatMap((p) => p.data ?? []);

  const byModel = series(buckets);
  const minutes = buckets.reduce((n, p) => n + (p.data ?? []).length, 0);
  console.log(`${minutes} minute bucket(s), ${Object.keys(byModel).length} model(s), `
              + `${groups.length} rate limit group(s)`);

  let findings = 0;
  for (const model of Object.keys(byModel).sort()) {
    const limits = groupForModel(groups, model);
    const [state, detail, facts] = verdict(byModel[model], limits, model, rampThreshold);
    console.log(`${state.padEnd(21)} ${model}: ${detail}`);

    if (['acceleration-suspect', 'ramp-near-ceiling', 'steady'].includes(state)) {
      const pct = (s) => (s === null ? 'unknown' : `${Math.round(s * 100)}%`);
      console.log(`  peak input   ${fmt(facts.peakIn[1])}/min against ITPM `
                  + `${fmt(facts.itpm ?? 0)} (${pct(facts.inShare)})`);
      console.log(`  peak output  ${fmt(facts.peakOut[1])}/min against OTPM `
                  + `${fmt(facts.otpm ?? 0)} (${pct(facts.outShare)})`);
      if (facts.ramps.length) {
        const [prevAt, at, factor, prev, current] = facts.ramps[0];
        console.log(`  steepest ramp ${factor.toFixed(1)}x between `
                    + `${prevAt.slice(11, 16) || prevAt} and ${at.slice(11, 16) || at} `
                    + `(${fmt(prev)} -> ${fmt(current)})`);
      }
      console.log('  note: this report carries no request count, so the ramp above '
                  + 'is measured in tokens. Sub-minute bursting is invisible here.');
    }

    for (const line of repairLines(state, facts)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;

    for (const [ltype, configured, floor] of belowPublishedStart(model, limits)) {
      console.log(`${'below-published-start'.padEnd(21)} ${model}: configured `
                  + `${ltype} is ${fmt(configured)}, under the published Start tier `
                  + `figure of ${fmt(floor)}`);
      for (const line of repairLines('below-published-start')) {
        console.log(`  repair: ${line}`);
      }
      findings += 1;
    }
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
