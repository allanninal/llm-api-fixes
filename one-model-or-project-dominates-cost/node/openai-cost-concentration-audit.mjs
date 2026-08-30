/**
 * Rank OpenAI organization spend by line item and by project.
 *
 * Read only. Two GET requests against /v1/organization/costs, which rejects
 * project keys: this needs an organization admin key (sk-admin-), provisioned
 * read-only. Nothing here is broken; the finding is where the money is.
 */
const API = 'https://api.openai.com/v1';

// group_by on the costs endpoint takes only these. Not model: the model name
// lives inside the line_item string, next to the token side.
const AXES = ['line_item', 'project_id'];

// quantity_unit is a small enumeration and only two members are tokens.
const TOKENS_PER_UNIT = { tokens: 1.0, '1000_tokens': 1000.0 };

const FINDINGS = ['dominant', 'top-heavy', 'unattributable'];

/**
 * Aggregate a grouped cost report by one field. Pure. Rows come back sorted by
 * dollars descending with each row's share of the total. A row whose name is
 * null keeps a null name: turning it into "unknown" would hide that the report
 * answered precisely, and that the answer was "this belongs to no project".
 */
export function rank(buckets, field) {
  const rows = new Map();
  for (const bucket of buckets ?? []) {
    for (const result of bucket.results ?? []) {
      const raw = result[field];
      const name = (typeof raw === 'string' && raw.trim()) ? raw.trim() : null;
      const row = rows.get(name)
        ?? { name, amount: 0, quantity: 0, unit: null };
      const amount = Number(result.amount?.value ?? 0);
      if (Number.isFinite(amount)) row.amount += amount;
      const quantity = Number(result.quantity ?? 0);
      if (Number.isFinite(quantity)) row.quantity += quantity;
      const rawUnit = result.quantity_unit;
      const unit = (typeof rawUnit === 'string' && rawUnit.trim())
        ? rawUnit.trim() : null;
      if (unit && row.unit === null) row.unit = unit;
      else if (unit && row.unit !== unit && row.unit !== 'mixed') row.unit = 'mixed';
      rows.set(name, row);
    }
  }

  const total = [...rows.values()].reduce((a, row) => a + row.amount, 0);
  const out = [...rows.values()].map((row) => ({
    ...row,
    amount: Math.round(row.amount * 100) / 100,
    share: total > 0 ? Math.round((row.amount / total) * 10000) / 10000 : 0,
  }));
  out.sort((a, b) => (b.amount - a.amount)
    || String(a.name ?? '').localeCompare(String(b.name ?? '')));
  return out;
}

/**
 * Dollars per million tokens for one row, or null. Pure. null for every unit
 * that is not tokens: a row billed in images or gibibyte-hours has a perfectly
 * good unit price and it is not a token price, so printing one would invent a
 * number that looks comparable to the rows around it and is not.
 */
export function unitPrice(amount, quantity, unit) {
  const scale = TOKENS_PER_UNIT[String(unit ?? '').trim().toLowerCase()];
  if (scale === undefined) return null;
  const tokens = (Number(quantity) || 0) * scale;
  const dollars = Number(amount);
  if (!Number.isFinite(tokens) || !Number.isFinite(dollars) || tokens <= 0) {
    return null;
  }
  return Math.round((dollars / (tokens / 1000000)) * 10000) / 10000;
}

/**
 * Classify one axis of a ranking. Pure. Returns [state, detail]. "spread" is an
 * answer rather than a failure to find something, and "unattributable" is kept
 * apart from "dominant" because no model substitution fixes it.
 */
export function verdict(ranked, threshold = 0.50, pairThreshold = 0.75,
                        minSpend = 1.0) {
  const rows = (ranked ?? []).map((row) => ({ ...row }));
  const total = Math.round(rows.reduce((a, row) => a + (Number(row.amount) || 0), 0)
    * 100) / 100;
  if (rows.length === 0 || total < minSpend) {
    return ['no-spend',
      `$${total.toFixed(2)} across ${rows.length} row(s), too little to rank`];
  }

  const top = rows[0];
  const share = (Number(top.amount) || 0) / total;
  const name = top.name ?? null;
  const shown = name === null ? 'null' : JSON.stringify(name);

  if (name === null && share >= threshold) {
    return ['unattributable',
      `${Math.round(share * 100)}% of $${total.toFixed(2)} is on a row the ` +
      'report returned with no name. Null is not unknown here: this axis ' +
      'cannot attribute that spend, which is a problem to fix before the cost ' +
      'is one to argue about.'];
  }

  if (share >= threshold) {
    return ['dominant',
      `${shown} is ${Math.round(share * 100)}% of $${total.toFixed(2)}. ` +
      `Optimising anything else moves at most ${Math.round((1 - share) * 100)}% ` +
      'of the bill.'];
  }

  if (rows.length > 1) {
    const second = (Number(rows[1].amount) || 0) / total;
    if (share + second >= pairThreshold) {
      const other = rows[1].name === null ? 'null' : JSON.stringify(rows[1].name);
      return ['top-heavy',
        `${shown} and ${other} are ${Math.round((share + second) * 100)}% of ` +
        `$${total.toFixed(2)} between them, with neither above ` +
        `${Math.round(threshold * 100)}% alone.`];
    }
  }

  return ['spread',
    `no single row above ${Math.round(threshold * 100)}% of $${total.toFixed(2)} ` +
    `across ${rows.length} row(s)`];
}

async function get(key, params) {
  const url = new URL(`${API}/organization/costs`);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.forEach((one) => url.searchParams.append(k, String(one)));
    else if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: /v1/organization/costs needs an ` +
                    'organization admin key (sk-admin-), not a project key');
  }
  if (!res.ok) throw new Error(`${res.status} from /organization/costs`);
  return res.json();
}

async function readBuckets(key, params, maxPages = 40) {
  const out = [];
  let query = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, query);
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
  const threshold = Number((process.env.THRESHOLD || "dummy-threshold") ?? 0.50);
  const top = Number((process.env.TOP || "dummy-top") ?? 5);
  const start = Math.floor(Date.now() / 1000) - days * 86400;

  let found = 0;
  for (const axis of AXES) {
    const rows = rank(await readBuckets(key, {
      start_time: start,
      bucket_width: '1d',
      limit: Math.min(180, Math.max(1, days)),
      group_by: [axis],
    }), axis);
    const [state, detail] = verdict(rows, threshold);
    const line = `${axis.padEnd(11)} ${state.padEnd(13)} ${detail}`;

    if (FINDINGS.includes(state)) {
      found += 1;
      console.warn(line);
    } else {
      console.log(line);
    }

    for (const row of rows.slice(0, top)) {
      const price = unitPrice(row.amount, row.quantity, row.unit);
      console.log(`    ${String(row.name ?? '(no name)').padEnd(38)} ` +
        `$${row.amount.toFixed(2)}  ${(row.share * 100).toFixed(1)}%  ` +
        (price === null
          ? `${row.unit ?? 'no unit'}, not a token unit`
          : `$${price.toFixed(2)} per 1M tokens`));
    }

    if (state === 'dominant' && axis === 'line_item') {
      console.warn(`  repair: price the substitute for ${JSON.stringify(rows[0].name)} ` +
        'and run the comparison before optimising anything else. Output tokens ' +
        'are the expensive side on every current model, and a smaller model at ' +
        'the same volume is usually a multiple cheaper rather than a few percent.');
    } else if (state === 'dominant' && axis === 'project_id') {
      console.warn(`  repair: give project ${JSON.stringify(rows[0].name)} its own ` +
        'spend limit and its own owner. A project this size behind the ' +
        "organization's single ceiling means one loop in it can stop everybody " +
        "else's traffic.");
    } else if (state === 'unattributable') {
      console.warn(`  repair: this spend belongs to no ${axis}. Move the traffic ` +
        'onto named projects and keys before treating any per-team number as real.');
    }
  }

  console.log(`2 axis/axes ranked, ${found} with a concentrated bill`);
  process.exitCode = found ? 1 : 0;
}

// Only run when invoked directly, so importing this module from the test file
// does not fire main() and fail on the missing key.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
