/**
 * Find the cost jump that is reasoning tokens rather than traffic or prompts.
 *
 * Read only. Two GET requests and nothing else: OPENAI_ADMIN_KEY must be an
 * organization admin key with read scopes, because /v1/organization endpoints
 * reject project keys. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

/**
 * Sum a list of usage buckets into one row. Pure. Anthropic's messages usage
 * report carries no request count, so requests comes back 0 there and the
 * verdict falls back to a weaker ratio rather than dividing by nothing.
 */
export function totals(buckets) {
  const row = { requests: 0, input: 0, output: 0, buckets: 0 };
  for (const b of buckets) {
    row.buckets += 1;
    for (const r of b.results ?? []) {
      row.requests += Number(r.num_model_requests ?? 0);
      row.input += Number(r.input_tokens ?? r.uncached_input_tokens ?? 0);
      row.output += Number(r.output_tokens ?? 0);
    }
  }
  return row;
}

/**
 * Cut a daily series into [prior, recent] around a boundary. Pure, clock passed
 * in, so a test's boundary is a date you can read. Buckets older than twice the
 * window are dropped: last week against a quarter ago is a different question.
 */
export function split(buckets, now, windowDays = 7) {
  const edge = now.getTime() / 1000 - windowDays * 86400;
  const floor = now.getTime() / 1000 - 2 * windowDays * 86400;
  const prior = [];
  const recent = [];
  for (const b of buckets) {
    if (typeof b.start_time !== 'number') continue;
    if (b.start_time >= edge) recent.push(b);
    else if (b.start_time >= floor) prior.push(b);
  }
  return [prior, recent];
}

/**
 * Say which of the four explanations for a cost jump the numbers support. Pure.
 * Returns [state, detail].
 */
export function verdict(prior, recent, jump = 1.5, flat = 0.2) {
  const a = totals(prior);
  const b = totals(recent);

  if (!b.requests && !b.output) return ['no-data', 'no usage in the recent window'];

  if (b.requests && !b.output) {
    return ['failing-before-generation',
      `${b.requests} request(s) in the recent window generated zero output ` +
      'tokens. Those calls were rejected before the model ran; that is an error ' +
      'shape and not a reasoning one.'];
  }

  if (!a.requests || !b.requests) {
    if (a.input && b.input) {
      const before = a.output / a.input;
      const after = b.output / b.input;
      if (before && after / before >= jump) {
        return ['unmeasurable-but-rising',
          'no request count in these buckets, so this is output per input token, ' +
          `not per request: ${before.toFixed(2)} to ${after.toFixed(2)}. ` +
          'Consistent with reasoning, but prompt shrinkage looks identical.'];
      }
      return ['unmeasurable',
        `no request count in these buckets. Output per input token is ` +
        `${after.toFixed(2)} against ${before.toFixed(2)} before, which is the ` +
        'strongest claim available without a request count.'];
    }
    return ['unmeasurable', 'no request count and no input tokens to fall back on'];
  }

  const inBefore = a.input / a.requests;
  const inAfter = b.input / b.requests;
  const outBefore = a.output / a.requests;
  const outAfter = b.output / b.requests;
  const numbers = `${outBefore.toFixed(0)} to ${outAfter.toFixed(0)} output ` +
    `tokens per request, ${inBefore.toFixed(0)} to ${inAfter.toFixed(0)} input`;

  const outFactor = outBefore ? outAfter / outBefore : 0;
  const inFactor = inBefore ? inAfter / inBefore : 0;

  if (outFactor >= jump && Math.abs(inFactor - 1) <= flat) {
    return ['reasoning-tax',
      `${numbers}. Output per request rose ${outFactor.toFixed(1)}x while input ` +
      'per request held steady. Those tokens were generated and billed at the ' +
      'output rate and never returned to you.'];
  }

  if (outFactor >= jump && inFactor >= jump) {
    return ['longer-prompts',
      `${numbers}. Both ratios rose together, so the prompts grew. Raising ` +
      'reasoning effort does not move the input side.'];
  }

  if (b.requests >= a.requests * jump) {
    return ['volume-only',
      `${numbers}. Requests rose from ${a.requests} to ${b.requests} with the ` +
      'ratios unchanged: the bill grew because traffic grew, and unit economics ' +
      'did not move.'];
  }

  return ['steady', numbers];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of params) url.searchParams.append(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization endpoints need ` +
                    'an organization admin key, not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${url.pathname}`);
  return res.json();
}

export async function usageByModel(key, since, days) {
  let params = [['start_time', String(since)], ['bucket_width', '1d'],
    ['limit', String(Math.max(days, 1))], ['group_by[]', 'model']];
  const out = new Map();
  for (;;) {
    const page = await get(key, '/organization/usage/completions', params);
    for (const b of page.data ?? []) {
      for (const r of b.results ?? []) {
        const model = r.model ?? 'unspecified';
        if (!out.has(model)) out.set(model, []);
        out.get(model).push({ start_time: b.start_time, results: [r] });
      }
    }
    if (!page.has_more || !page.next_page) break;
    params = params.filter((p) => p[0] !== 'page').concat([['page', page.next_page]]);
  }
  return out;
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key with read ' +
                  'scopes; project keys are rejected by /v1/organization/*)');
    process.exitCode = 2;
    return;
  }

  const argv = process.argv;
  const days = Number(argv.includes('--days') ? argv[argv.indexOf('--days') + 1] : 30) || 30;
  const win = Number(argv.includes('--window') ? argv[argv.indexOf('--window') + 1] : 7) || 7;

  const now = new Date();
  const since = Math.floor(now.getTime() / 1000 - days * 86400);
  const byModel = await usageByModel(admin, since, days);
  if (byModel.size === 0) {
    console.log(`no completion usage in the last ${days} day(s)`);
    return;
  }

  let bad = 0;
  for (const [model, buckets] of [...byModel.entries()].sort()) {
    const [prior, recent] = split(buckets, now, win);
    const [state, detail] = verdict(prior, recent);
    const line = `${model.padEnd(22)} ${state.padEnd(26)} ${detail}`;
    if (['steady', 'volume-only', 'no-data', 'unmeasurable'].includes(state)) {
      console.log(line);
      continue;
    }
    bad += 1;
    console.warn(line);
    if (state === 'reasoning-tax' || state === 'unmeasurable-but-rising') {
      console.warn('  repair: lower the reasoning effort on this model for tasks ' +
                   'that do not need deliberation, and drop the higher modes ' +
                   'unless an eval justifies them. Log ' +
                   'usage.output_tokens_details.reasoning_tokens per call so the ' +
                   'invisible half shows up in your own metrics.');
      console.warn(`  cross-check the money: GET ${API}/organization/costs` +
                   `?start_time=${since}&bucket_width=1d&group_by[]=line_item`);
    }
  }

  console.log(`${byModel.size} model(s) over ${days} day(s), ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
