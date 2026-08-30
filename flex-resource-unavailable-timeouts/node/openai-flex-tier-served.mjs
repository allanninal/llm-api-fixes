/**
 * Find flex tier work that was never served, and flex you never actually asked for.
 *
 * Read only. One paged GET of the completions usage report, grouped by
 * service_tier and model. The tier on each result is the tier the request was
 * actually served on, which is what makes this readable at all.
 *
 * A 429 Resource Unavailable is explicitly not charged, so it never appears in
 * any usage report: the evidence is a hole. The gap test is deliberately
 * conservative because absence is one inference further from the data than
 * everything else in this section.
 *
 * The cost report cannot substitute: its group_by accepts project_id, line_item
 * and api_key_id and has no service tier dimension at all.
 */
const API = 'https://api.openai.com/v1';
const FLEX = 'flex';

export const MIN_SERVED_HOURS = 6;
const FINDINGS = new Set(['flex-never-served', 'flex-shortfall']);

/** A number, or 0. Pure. */
export function num(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

const key = (model, tier) => `${model}\u0000${tier}`;

/** {"model\0tier": {hour: {requests, input, output}}}. Pure. */
export function tierRows(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const bucket of page?.data ?? []) {
      const hour = Math.trunc(num(bucket?.start_time));
      for (const result of bucket?.results ?? []) {
        const k = key(String(result?.model ?? '(all models)'),
                      String(result?.service_tier ?? '(untiered)'));
        const hours = (out[k] ??= {});
        const row = (hours[hour] ??= { requests: 0, input: 0, output: 0 });
        row.requests += num(result?.num_model_requests);
        row.input += num(result?.input_tokens);
        row.output += num(result?.output_tokens);
      }
    }
  }
  return out;
}

/** {tier: total requests}. Pure. */
export function totalsByTier(rows) {
  const out = {};
  for (const [k, hours] of Object.entries(rows ?? {})) {
    const tier = k.split('\u0000')[1];
    out[tier] = (out[tier] ?? 0)
      + Object.values(hours).reduce((n, h) => n + h.requests, 0);
  }
  return out;
}

/** {hour: requests served across every tier}. Pure. The control. */
export function hoursActive(rows) {
  const out = {};
  for (const hours of Object.values(rows ?? {})) {
    for (const [hour, counts] of Object.entries(hours)) {
      out[hour] = (out[hour] ?? 0) + counts.requests;
    }
  }
  return out;
}

/** The median of a list. Pure. 0 when empty. */
export function median(values) {
  const ordered = [...(values ?? [])].map(Number).sort((a, b) => a - b);
  if (ordered.length === 0) return 0;
  const mid = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[mid] : (ordered[mid - 1] + ordered[mid]) / 2;
}

/** {hour: flex requests} for one model. Pure. */
export function flexByHour(rows, model) {
  const hours = (rows ?? {})[key(model, FLEX)] ?? {};
  return Object.fromEntries(Object.entries(hours).map(([h, c]) => [h, c.requests]));
}

/** {tier: requests} for one model across every tier. Pure. */
export function tiersForModel(rows, model) {
  const out = {};
  for (const [k, hours] of Object.entries(rows ?? {})) {
    const [candidate, tier] = k.split('\u0000');
    if (candidate !== model) continue;
    out[tier] = (out[tier] ?? 0)
      + Object.values(hours).reduce((n, h) => n + h.requests, 0);
  }
  return out;
}

/** [[hour, flexRequests, otherRequests, median]] where flex collapsed. Pure. */
export function flexGaps(flexHours, active, floor = 0.5, minServed = MIN_SERVED_HOURS) {
  const served = Object.values(flexHours ?? {}).filter((v) => v > 0);
  if (served.length < minServed) return [];
  const mid = median(served);
  if (mid <= 0) return [];
  const out = [];
  for (const hour of Object.keys(active ?? {}).sort((a, b) => Number(a) - Number(b))) {
    const flex = Number((flexHours ?? {})[hour] ?? 0);
    const other = Number(active[hour]) - flex;
    if (flex <= mid * floor && other > 0) out.push([Number(hour), flex, other, mid]);
  }
  out.sort((a, b) => (a[1] - b[1]) || (a[0] - b[0]));
  return out;
}

/** [[model, 0, {tier: requests}]] for configured models with no flex rows. Pure. */
export function neverServed(rows, configured) {
  const out = [];
  const models = [...new Set((configured ?? []).filter(Boolean).map(String))].sort();
  for (const model of models) {
    const tiers = tiersForModel(rows, model);
    if ((tiers[FLEX] ?? 0) > 0) continue;
    if (Object.values(tiers).reduce((n, v) => n + v, 0) <= 0) continue;
    out.push([model, 0, tiers]);
  }
  return out;
}

/** Thousands separators. Pure. */
export function fmt(value) {
  return Math.round(num(value)).toLocaleString('en-US');
}

/** Classify one model. Pure. Returns [state, detail]. */
export function verdict(model, flexHours, gaps, tiers, configured) {
  const byTier = tiers ?? {};
  const flexTotal = byTier[FLEX] ?? 0;
  const otherTotal = Object.entries(byTier)
    .filter(([t]) => t !== FLEX).reduce((n, [, v]) => n + v, 0);
  if (flexTotal <= 0 && (configured ?? []).includes(model)) {
    if (otherTotal <= 0) return ['no-usage', 'no requests on any tier in this window'];
    return ['flex-never-served',
            `${fmt(flexTotal)} flex request(s) in this window, and ${fmt(otherTotal)} `
            + 'on other tiers. The service_tier parameter is not reaching the API.'];
  }
  if (flexTotal <= 0) return ['no-flex-usage', 'never served on flex in this window'];
  if ((gaps ?? []).length) {
    return ['flex-shortfall',
            `${gaps.length} hour(s) at or below half the median served hour `
            + `(median ${fmt(gaps[0][3])} requests)`];
  }
  const served = Object.values(flexHours ?? {}).filter((v) => v > 0).length;
  if (served < MIN_SERVED_HOURS) {
    return ['too-little-history',
            `${served} hour(s) of flex traffic, which is not enough to take a median from`];
  }
  return ['flex-served',
          `${fmt(flexTotal)} flex request(s) across ${served} hour(s), no collapsed hours`];
}

/** An hour bucket's start as a readable UTC string. Pure. */
export function stamp(hour) {
  return `${new Date(Number(hour) * 1000).toISOString().slice(0, 13)}:00Z`;
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'flex-never-served') {
    return ['the tier in this report is the tier that was served. Check for a '
      + 'gateway that rewrites request bodies, an SDK wrapper with its own '
      + 'defaults, or a code path that never set service_tier at all.',
      'until it arrives you are paying standard rates for a workload you believe '
      + 'is discounted, and nothing will raise about it.'];
  }
  if (state === 'flex-shortfall') {
    return ['back off and retry on 429 Resource Unavailable, which means no '
      + 'capacity right now rather than a limit you exceeded. Retrying it '
      + 'genuinely helps, unlike the billing 429s.',
      'raise the client timeout to at least 15 minutes. The official SDK default '
      + 'is 10 and flex responses regularly exceed it, and an aborted request can '
      + 'still be billed if the server finishes.',
      'fall back to service_tier auto when completing the work matters more than '
      + 'the discount, and keep flex off anything a person is waiting for.'];
  }
  if (state === 'too-little-history') {
    return ['not a clean bill of health, just too little to read. Re-run over a '
      + 'longer window once the job has more served hours behind it.'];
  }
  return [];
}

async function read(apiKey, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params ?? {})) {
    for (const one of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(one));
  }
  const r = await fetch(url, { headers: { Authorization: `Bearer ${apiKey}`,
                                          'User-Agent': 'openai-flex-tier-served/1.0' } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: the organization usage endpoints need `
                    + 'an admin key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function main() {
  const apiKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!apiKey) {
    console.error('set OPENAI_ADMIN_KEY to an admin key that can read the '
                  + 'organization usage endpoints');
    process.exitCode = 2;
    return;
  }
  const days = Math.max(0.5, Math.min(7, Number((process.env.DAYS || "dummy-days") ?? 7)));
  const floor = Number((process.env.FLOOR || "dummy-floor") ?? 0.5);
  const configured = ((process.env.FLEX_MODELS || "dummy-flex-models") ?? '').split(/[,\s]+/).filter(Boolean);

  const start = Math.floor(Date.now() / 1000 - days * 86400);
  const payloads = [];
  const params = { start_time: start, bucket_width: '1h', limit: 168,
                   'group_by[]': ['service_tier', 'model'] };
  for (let i = 0; i < 50; i += 1) {
    const page = await read(apiKey, '/organization/usage/completions', params);
    payloads.push(page);
    if (!page.has_more || !page.next_page) break;
    params.page = page.next_page;
  }

  const rows = tierRows(payloads);
  const totals = totalsByTier(rows);
  const active = hoursActive(rows);
  const buckets = payloads.reduce((n, p) => n + (p.data ?? []).length, 0);
  console.log(`${buckets} hourly bucket(s), ${Object.keys(totals).length} tier(s) `
              + `observed: ${Object.keys(totals).sort().join(', ') || 'none'}`);
  if (configured.length === 0) {
    console.log('no FLEX_MODELS given, so the never-served check is skipped: nothing '
                + 'the API returns knows which models your code asks for flex on');
  }

  let findings = 0;
  const models = [...new Set([...Object.keys(rows).map((k) => k.split('\u0000')[0]),
                              ...configured])].sort();
  for (const model of models) {
    const flexHours = flexByHour(rows, model);
    const gaps = flexGaps(flexHours, active, floor);
    const tiers = tiersForModel(rows, model);
    const [state, detail] = verdict(model, flexHours, gaps, tiers, configured);
    if (['no-flex-usage', 'no-usage'].includes(state) && !configured.includes(model)) {
      continue;
    }
    console.log(`${state.padEnd(21)} ${model}: ${detail}`);
    for (const [hour, flex, other] of gaps.slice(0, 5)) {
      console.log(`  ${stamp(hour)}  ${fmt(flex)} requests, other tiers served `
                  + `${fmt(other)} that hour`);
    }
    if (state === 'flex-shortfall') {
      console.log('  note: a 429 Resource Unavailable is not charged and never reaches '
                  + 'this report, so these hours are absence rather than error counts.');
    }
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
