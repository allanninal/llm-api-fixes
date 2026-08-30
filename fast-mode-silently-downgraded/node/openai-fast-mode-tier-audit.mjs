/**
 * Report OpenAI projects whose configured service tier and invoice disagree.
 *
 * Read only. Two GET requests against the organization endpoints and nothing
 * else. Those endpoints reject project keys, so this needs an organization
 * admin key (sk-admin-), which can and should be provisioned read-only.
 */
const API = 'https://api.openai.com/v1';

// Fast mode is priced at twice the standard rate. The multiplier describes the
// finding; the dollars come from the cost report rather than a price table.
const PREMIUM_MULTIPLIER = 2.0;

// line_item is a human-readable label, not a documented enum.
const PREMIUM_WORDS = ['fast', 'priority'];

// What the project object calls the setting the console calls Project Service
// Tier. Read leniently and in this order; absent is reported as absent.
const TIER_FIELDS = ['service_tier', 'default_service_tier'];

const FINDINGS = ['downgraded', 'partly-downgraded', 'unrequested-premium'];

/**
 * Read a project's configured service tier. Pure. Returns a lowercase string,
 * or null when the object carries no such field. null is not "standard": a
 * missing field means the setting is unreadable here, and treating that as a
 * configured default would turn every unreadable project into a false clean.
 */
export function tierOf(project) {
  const candidates = TIER_FIELDS.map((f) => project[f]);
  const settings = project.settings;
  if (settings !== null && typeof settings === 'object' && !Array.isArray(settings)) {
    for (const f of TIER_FIELDS) candidates.push(settings[f]);
  }
  for (const value of candidates) {
    if (typeof value === 'string' && value.trim()) return value.trim().toLowerCase();
  }
  return null;
}

/**
 * Split one project's spend into premium and standard dollars. Pure. Returns
 * [premium, standard, labels]; the labels are the distinct line_item strings
 * that matched, so the report can show what the substring test caught.
 */
export function splitSpend(buckets, projectId) {
  let premium = 0;
  let standard = 0;
  const labels = new Set();
  for (const bucket of buckets ?? []) {
    for (const result of bucket.results ?? []) {
      if (String(result.project_id ?? '') !== String(projectId)) continue;
      const label = String(result.line_item ?? '');
      const value = Number(result.amount?.value ?? 0);
      if (!Number.isFinite(value)) continue;
      const low = label.toLowerCase();
      if (PREMIUM_WORDS.some((w) => low.includes(w))) {
        premium += value;
        if (value) labels.add(label);
      } else {
        standard += value;
      }
    }
  }
  return [Math.round(premium * 100) / 100, Math.round(standard * 100) / 100,
          [...labels].sort()];
}

/**
 * Parse --tier project_id=tier arguments into a Map. Pure. For organizations
 * whose project objects do not carry the setting: you read it once in the
 * console and hand it over, rather than the script guessing.
 */
export function overrides(pairs) {
  const out = new Map();
  for (const pair of pairs ?? []) {
    const text = String(pair);
    const at = text.indexOf('=');
    if (at < 0) continue;
    const name = text.slice(0, at).trim();
    const value = text.slice(at + 1).trim().toLowerCase();
    if (name && value) out.set(name, value);
  }
  return out;
}

/**
 * Classify one project. Pure. Returns [state, detail]. The two findings are
 * opposite and are never collapsed: one costs latency you thought you bought,
 * the other costs money nobody budgeted.
 */
export function verdict(tier, premium, standard, minSpend = 1.0, delivered = 0.60) {
  const prem = Math.max(0, Number(premium) || 0);
  const std = Math.max(0, Number(standard) || 0);
  const total = prem + std;
  const configured = (tier ?? '').trim().toLowerCase() || null;

  if (total < minSpend) {
    return ['no-spend',
      `$${total.toFixed(2)} of spend in the window, too little to say anything ` +
      'about which tier served it'];
  }

  const share = prem / total;
  const pct = Math.round(share * 100);

  if (configured === 'fast' || configured === 'priority') {
    if (prem <= 0) {
      return ['downgraded',
        `configured for the ${configured} tier and not one dollar of ` +
        `$${total.toFixed(2)} in spend is on a premium line item. Every ` +
        'request in the window was served on the default tier.'];
    }
    if (share < delivered) {
      return ['partly-downgraded',
        `configured for the ${configured} tier, and only ${pct}% of ` +
        `$${total.toFixed(2)} in spend is on premium line items. The rest was ` +
        'downgraded and served at default latency.'];
    }
    return ['premium-delivered',
      `configured for the ${configured} tier and ${pct}% of $${total.toFixed(2)} ` +
      `is billed at it. The premium is being delivered and charged at about ` +
      `${PREMIUM_MULTIPLIER.toFixed(1)}x the standard rate, so somebody should ` +
      'still want it.'];
  }

  if (configured === null) {
    if (prem > 0) {
      return ['unknown-tier-premium',
        `the project object carries no readable service tier and ` +
        `$${prem.toFixed(2)} of $${total.toFixed(2)} is on premium line items. ` +
        'Read the setting in the console and pass it with --tier.'];
    }
    return ['unknown-tier',
      'the project object carries no readable service tier. No premium line ' +
      `items in $${total.toFixed(2)} of spend, so nothing is being billed at ` +
      'the premium rate today.'];
  }

  if (prem > 0) {
    return ['unrequested-premium',
      `the project tier is ${configured} and ${pct}% of $${total.toFixed(2)} is ` +
      'on premium line items, so a code path is sending the tier in the request ' +
      `body. That traffic bills at about ${PREMIUM_MULTIPLIER.toFixed(1)}x the ` +
      'standard rate.'];
  }
  return ['standard',
    `tier is ${configured} and no premium line items in $${total.toFixed(2)} of spend`];
}

async function get(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.forEach((one) => url.searchParams.append(k, String(one)));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/* needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* walkProjects(key, pageSize, maxPages) {
  let params = { limit: pageSize };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/projects', params);
    const data = page.data ?? [];
    for (const project of data) yield project;
    if (!page.has_more || data.length === 0) return;
    params = { limit: pageSize, after: data[data.length - 1].id };
  }
}

async function costPages(key, params, maxPages = 40) {
  const out = [];
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/organization/costs', query);
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) break;
    query = { ...params, page: page.next_page };
  }
  return out;
}

async function main() {
  const key = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key") ?? (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_ADMIN_KEY (an organization admin key, read-only ' +
                  'scopes are enough)');
    process.exitCode = 2;
    return;
  }

  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const minSpend = Number((process.env.MIN_SPEND || "dummy-min-spend") ?? 1.0);
  const delivered = Number((process.env.DELIVERED || "dummy-delivered") ?? 0.60);
  const showAll = process.argv.includes('--show-all');
  const told = overrides(process.argv
    .filter((a) => a.startsWith('--tier='))
    .map((a) => a.slice('--tier='.length)));

  const costs = await costPages(key, {
    start_time: Math.floor(Date.now() / 1000) - days * 86400,
    bucket_width: '1d',
    limit: Math.min(180, Math.max(1, days)),
    group_by: ['line_item', 'project_id'],
  });

  let checked = 0;
  let found = 0;
  for await (const project of walkProjects(key, 100, 20)) {
    const projectId = String(project.id ?? '');
    const name = String(project.name ?? projectId);
    const tier = told.get(projectId) ?? tierOf(project);
    const [premium, standard, labels] = splitSpend(costs, projectId);
    const [state, detail] = verdict(tier, premium, standard, minSpend, delivered);
    checked += 1;
    const line = `${state.padEnd(21)} ${projectId} (${name})  ${detail}`;

    if (FINDINGS.includes(state)) {
      found += 1;
      console.warn(line);
      if (labels.length) {
        console.warn(`  matched premium line item(s): ${labels.join(', ')}`);
      }
      if (state === 'unrequested-premium') {
        console.warn('  repair: find the call site sending the tier in the ' +
          'request body and drop it, or budget for it deliberately. Nothing in ' +
          'the project settings asked for this.');
      } else {
        console.warn('  repair: either stop paying for a tier you are not being ' +
          'served (set Project Service Tier back to standard) or ask OpenAI to ' +
          'raise the ramp limits that are downgrading you. Decide which, then ' +
          'log the response envelope\'s service_tier so the downgrade rate is a ' +
          'metric instead of an audit.');
      }
    } else if (state === 'unknown-tier' || state === 'unknown-tier-premium') {
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${checked} project(s) checked, ${found} with a tier the invoice ` +
              'disagrees with');
  process.exitCode = found ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
