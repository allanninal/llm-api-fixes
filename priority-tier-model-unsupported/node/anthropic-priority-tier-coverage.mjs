/**
 * Find Claude models that never report Priority Tier service.
 *
 * Read only. One paged GET against the messages usage report with an Admin API
 * key. Nothing is sent to /v1/messages.
 *
 * The finding is coverage, not misconfiguration, and no dollar figure is
 * printed: Priority Tier costs are excluded from the cost report, so there is
 * no read-only source for the money on this surface.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const PRIORITY = 'priority';
const STANDARD = 'standard';
const BATCH = 'batch';
const UNKNOWN = 'unknown';
const TIERS = [PRIORITY, STANDARD, BATCH, UNKNOWN];

// Documented as NOT supported by Priority Tier. Family fragments carry their
// leading hyphen so that "-opus-5" cannot match claude-opus-4-5.
const UNSUPPORTED_FAMILIES = ['-opus-5', '-sonnet-5', '-mythos-5', '-mythos-preview'];

const BURNDOWN = ['cache reads 0.1x', '5-minute cache writes 1.25x',
                  '1-hour cache writes 2.0x', 'inference_geo us 1.1x on 4.6+'];

const FINDINGS = new Set(['unsupported-model', 'uncovered-model', 'partial-priority']);

/** Normalise the service_tier on one result row. Pure. Absent is never standard. */
export function tier(result) {
  const raw = String(result?.service_tier ?? '').trim().toLowerCase();
  return [PRIORITY, STANDARD, BATCH].includes(raw) ? raw : UNKNOWN;
}

/** Total billed tokens on one result row. Pure. cache_creation is an object. */
export function weigh(result) {
  const row = result ?? {};
  let total = 0;
  for (const field of ['uncached_input_tokens', 'cache_read_input_tokens',
                       'output_tokens']) {
    const n = Number(row[field] ?? 0);
    if (Number.isFinite(n)) total += Math.trunc(n);
  }
  if (row.cache_creation && typeof row.cache_creation === 'object') {
    for (const value of Object.values(row.cache_creation)) {
      const n = Number(value ?? 0);
      if (Number.isFinite(n)) total += Math.trunc(n);
    }
  }
  return total;
}

/** Sum tokens into { model: { tier: tokens } }. Pure. */
export function fold(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      for (const result of bucket?.results ?? []) {
        const model = String(result?.model ?? 'all models');
        if (!out[model]) {
          out[model] = Object.fromEntries(TIERS.map((t) => [t, 0]));
        }
        out[model][tier(result)] += weigh(result);
      }
    }
  }
  return out;
}

/** Is this model id on the documented Priority Tier exclusion list? Pure. */
export function isUnsupported(model) {
  const name = `-${String(model ?? '').trim().toLowerCase().replace(/^-+/, '')}`;
  return UNSUPPORTED_FAMILIES.some((fragment) => name.includes(fragment));
}

/**
 * Does any model in the window report priority tokens? Pure.
 * Run before any model is graded: an org with no commitment reports zero
 * everywhere, and grading a model against that is a finding about nothing.
 */
export function orgHasPriority(rows) {
  return Object.values(rows ?? {}).some((row) => Number(row?.[PRIORITY] ?? 0) > 0);
}

/** One tier's share of a model's billed tokens. Pure. 0 when empty. */
export function share(row, which) {
  const data = row ?? {};
  const total = TIERS.reduce((sum, t) => sum + Number(data[t] ?? 0), 0);
  if (total <= 0) return 0;
  return Number(data[which] ?? 0) / total;
}

/** Classify one model's tier coverage. Pure. Returns [state, detail]. */
export function verdict(model, row, hasPriority, minTokens = 1_000_000, thin = 0.6) {
  const data = row ?? {};
  const total = TIERS.reduce((sum, t) => sum + Number(data[t] ?? 0), 0);
  if (total < minTokens) {
    return ['low-volume',
      `${total} billed token(s) in the window, too few to conclude anything`];
  }
  if (!hasPriority) {
    return ['no-priority-in-org',
      `0% priority of ${(total / 1e6).toFixed(1)}M token(s), and no model in ` +
      'this organization reports priority either. That is an organization ' +
      'without a capacity commitment, not a gap on this model.'];
  }
  const got = share(data, PRIORITY);
  if (got <= 0) {
    if (isUnsupported(model)) {
      return ['unsupported-model',
        `0% priority of ${(total / 1e6).toFixed(1)}M token(s). Documented as ` +
        'not supported by Priority Tier, so service_tier auto is accepted ' +
        'here and served standard every time.'];
    }
    return ['uncovered-model',
      `0% priority of ${(total / 1e6).toFixed(1)}M token(s), and this id is ` +
      'not on the documented exclusion list. Something else is keeping it off ' +
      'the tier: standard_only on the request, a workspace outside the ' +
      'commitment, or capacity that never had headroom.'];
  }
  if (got < thin) {
    return ['partial-priority',
      `${(got * 100).toFixed(0)}% priority of ${(total / 1e6).toFixed(1)}M ` +
      'token(s). Eligible, and mostly over the committed tokens per minute, ' +
      'so the rest fell back to standard.'];
  }
  return ['priority-covered',
    `${(got * 100).toFixed(0)}% priority of ${(total / 1e6).toFixed(1)}M token(s)`];
}

/** The repair for one classified model. Pure. Printed, never performed. */
export function repairLines(state, model) {
  if (state === 'unsupported-model') {
    return [
      `this is coverage, not configuration: ${model} cannot be served on ` +
      'Priority Tier at all, whatever service_tier says.',
      'either move the latency-sensitive traffic to a covered model id, or ' +
      'accept standard here and stop planning around a tier that never ' +
      'applies to it.',
      'standard_only is the way to deliberately preserve commitment capacity ' +
      'for the models that can use it.',
    ];
  }
  if (state === 'uncovered-model') {
    return [
      'check the request side for standard_only, and check that the workspace ' +
      'sending this traffic is inside the commitment.',
      `the exclusion list is not the explanation for ${model}, so the answer ` +
      'is in your own configuration or in capacity.',
    ];
  }
  if (state === 'partial-priority') {
    return [
      'the commitment is sized below this traffic. Requests past the committed ' +
      'input and output tokens per minute fall back to standard automatically, ' +
      'and one that would breach the ordinary rate limits is declined rather ' +
      'than served.',
      `burndown against the commitment is not one token per token: ${BURNDOWN.join(', ')}.`,
    ];
  }
  return [];
}

/** Floor to midnight UTC: starting_at must sit on a bucket boundary. */
export function windowStart(days, now = new Date()) {
  const midnight = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                     now.getUTCDate()));
  midnight.setUTCDate(midnight.getUTCDate() - days);
  return `${midnight.toISOString().slice(0, 19)}Z`;
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
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minTokens = Number((process.env.MIN_TOKENS || "dummy-min-tokens") ?? 1_000_000);

  const base = [['starting_at', windowStart(days)],
                ['bucket_width', '1d'],
                ['limit', String(Math.min(days + 1, 31))],
                ['group_by[]', 'service_tier'],
                ['group_by[]', 'model']];
  const collected = [];
  let params = base;
  for (;;) {
    const page = await get(admin, '/organizations/usage_report/messages', params);
    collected.push(page);
    if (!page?.has_more || !page?.next_page) break;
    params = [...base, ['page', page.next_page]];
  }

  const rows = fold(collected);
  const models = Object.keys(rows);
  if (models.length === 0) {
    console.log(`no usage in the last ${days} day(s)`);
    return;
  }

  const hasPriority = orgHasPriority(rows);
  const covered = models.filter((m) => Number(rows[m][PRIORITY] ?? 0) > 0).length;
  if (hasPriority) {
    console.log(`org has priority traffic on ${covered} of ${models.length} ` +
                'model(s), so a per-model zero is meaningful');
  } else {
    console.warn('no model in this organization reported any priority token(s) ' +
                 'in the window. Capacity commitments are no longer available ' +
                 'to purchase, so this is an organization without one rather ' +
                 'than a gap on any single model.');
  }

  const weight = (m) => TIERS.reduce((sum, t) => sum + Number(rows[m][t] ?? 0), 0);
  let bad = 0;
  for (const model of models.sort((a, b) => weight(b) - weight(a))) {
    const [state, detail] = verdict(model, rows[model], hasPriority, minTokens);
    const line = `${state.padEnd(20)} ${model.padEnd(26)} ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      for (const repair of repairLines(state, model)) {
        console.warn(`  repair: ${repair}`);
      }
    } else {
      console.log(line);
    }
  }

  console.log(`${models.length} model(s) checked, ${bad} finding(s)`);
  console.log('no dollar figure: Priority Tier costs are excluded from the ' +
              'cost report, so tokens are the only read-only reading here');
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
