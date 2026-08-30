/**
 * Find a container whose rate limit was set below the organization's.
 *
 * Read only. Every request is a GET, on either or both providers. Nothing here
 * reads a response header and nothing here reads traffic: the subject is the
 * configured ceiling on a container, legible whether or not that container has
 * sent a single request this month.
 *
 * Anthropic returns each workspace override with org_limit beside value on the
 * same object. OpenAI's project.rate_limit object carries no organization value
 * at all, so the peer maximum across projects stands in for the tier and is
 * reported as the proxy it is. The repair is a write and is printed only.
 */
const ANTHROPIC = 'https://api.anthropic.com/v1';
const OPENAI = 'https://api.openai.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

export const LIMITER_ORDER = ['requests_per_minute', 'input_tokens_per_minute',
                              'output_tokens_per_minute'];

const SEVERITY = ['throttled-below-org', 'override-pinned-at-org', 'override-above-org',
                  'limiter-inherited', 'org-limit-unknown', 'override-in-range',
                  'no-override'];

const FINDINGS = new Set(['throttled-below-org', 'override-pinned-at-org',
                          'override-above-org', 'project-outlier']);

/** An integer, or null. Pure. null is a real answer and must survive. */
export function num(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/** A stable printable name for one rate limit group. Pure. */
export function groupLabel(entry) {
  const gtype = String(entry?.group_type ?? '').trim() || 'unknown_group';
  const models = (entry?.models ?? []).filter(Boolean).map(String).sort();
  if (models.length === 0) return gtype;
  const extra = models.length - 1;
  return `${gtype}:${models[0]}${extra ? ` +${extra}` : ''}`;
}

/** {limiterType: value} for one group entry. Pure. */
export function limitsOf(entry) {
  const out = {};
  for (const row of entry?.limits ?? []) {
    const ltype = String(row?.type ?? '').trim();
    const value = num(row?.value);
    if (ltype && value !== null) out[ltype] = value;
  }
  return out;
}

/** {groupLabel: {limiterType: value}} from the organization endpoint. Pure. */
export function orgIndex(pages) {
  const out = {};
  for (const page of pages ?? []) {
    for (const entry of page?.data ?? []) {
      out[groupLabel(entry)] = { ...(out[groupLabel(entry)] ?? {}), ...limitsOf(entry) };
    }
  }
  return out;
}

const rank = (t) => (LIMITER_ORDER.indexOf(t) === -1 ? LIMITER_ORDER.length
                                                     : LIMITER_ORDER.indexOf(t));

/** [[limiterType, value, orgLimit]] for one workspace group. Pure. */
export function overridesOf(entry) {
  const out = [];
  for (const row of entry?.limits ?? []) {
    const ltype = String(row?.type ?? '').trim();
    if (!ltype) continue;
    out.push([ltype, num(row?.value), num(row?.org_limit)]);
  }
  out.sort((a, b) => (rank(a[0]) - rank(b[0])) || a[0].localeCompare(b[0]));
  return out;
}

/** Thousands separators, or a dash for null. Pure. */
export function fmt(value) {
  if (value === null || value === undefined) return '-';
  return Math.trunc(Number(value)).toLocaleString('en-US');
}

/** Grade one workspace limiter against the organization value. Pure. */
export function gradeOverride(value, orgLimit, floor = 0.5) {
  if (value === null || value === undefined) {
    return ['no-override', 'inherits the organization value'];
  }
  if (orgLimit === null || orgLimit === undefined) {
    return ['org-limit-unknown',
            `value is ${fmt(value)} and the organization publishes no number for `
            + 'this limiter, so the override cannot be graded'];
  }
  if (value <= 0) {
    return ['throttled-below-org',
            `set to ${fmt(value)}, which stops this limiter in this container entirely`];
  }
  if (value > orgLimit) {
    return ['override-above-org',
            `${fmt(value)} is above the organization's ${fmt(orgLimit)}, and the `
            + 'organization limit applies anyway'];
  }
  if (value === orgLimit) {
    return ['override-pinned-at-org',
            `${fmt(value)}, equal to the organization value today, so it will not `
            + 'follow the next increase'];
  }
  const share = value / orgLimit;
  const detail = `${fmt(value)} of ${fmt(orgLimit)} (${Math.round(share * 100)}%)`;
  return [share <= floor ? 'throttled-below-org' : 'override-in-range', detail];
}

/** [[limiterType, orgValue]] the organization publishes and the group did not override. */
export function inheritedLimiters(entry, orgTypes) {
  const overridden = new Set(overridesOf(entry).filter((r) => r[1] !== null)
    .map((r) => r[0]));
  const rows = Object.entries(orgTypes ?? {}).filter(([t]) => !overridden.has(t));
  rows.sort((a, b) => (rank(a[0]) - rank(b[0])) || a[0].localeCompare(b[0]));
  return rows;
}

/** Roll one container's limiter states into a single word. Pure. */
export function verdict(states) {
  const present = new Set(states ?? []);
  for (const state of SEVERITY) if (present.has(state)) return state;
  return 'no-override';
}

/** {model: {projectId: {rpm, tpm}}}. Pure. */
export function openaiMatrix(byProject) {
  const out = {};
  for (const pid of Object.keys(byProject ?? {}).sort()) {
    for (const row of byProject[pid] ?? []) {
      const model = String(row?.model ?? '').trim();
      if (!model) continue;
      (out[model] ??= {})[String(pid)] = {
        rpm: num(row?.max_requests_per_1_minute),
        tpm: num(row?.max_tokens_per_1_minute),
      };
    }
  }
  return out;
}

/** [[model, projectId, dimension, value, peerMax]]. Pure. Worst first. */
export function openaiOutliers(matrix, floor = 0.5) {
  const out = [];
  for (const model of Object.keys(matrix ?? {}).sort()) {
    const projects = matrix[model];
    if (Object.keys(projects).length < 2) continue;
    for (const dim of ['rpm', 'tpm']) {
      const usable = Object.entries(projects)
        .map(([p, v]) => [p, v?.[dim]])
        .filter(([, v]) => v !== null && v !== undefined && v > 0);
      if (usable.length < 2) continue;
      const peerMax = Math.max(...usable.map(([, v]) => v));
      for (const [pid, value] of usable.sort((a, b) => a[0].localeCompare(b[0]))) {
        if (value <= peerMax * floor) out.push([model, pid, dim, value, peerMax]);
      }
    }
  }
  out.sort((a, b) => (a[3] / a[4]) - (b[3] / b[4])
    || a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
  return out;
}

/** The repair for one state. Pure. Printed, never performed. */
export function repairLines(state) {
  if (state === 'throttled-below-org') {
    return ['this container is capped well under the organization ceiling. On '
      + 'Anthropic open the workspace in the Console, Rate limits tab, and raise '
      + 'or remove the override; there is no write endpoint for it.',
      'check the container id against what production actually uses before '
      + 'raising anything. A staging id that followed the code into production '
      + 'is repaired by changing the id, not the limit.'];
  }
  if (state === 'override-pinned-at-org') {
    return ['an override equal to today\'s organization value is a pin, not a '
      + 'no-op. Delete the override so the container follows the next tier '
      + 'increase instead of staying on this number.',
      'if the equality is deliberate, write it down somewhere the next tier '
      + 'increase will be read, because nothing in the API will mention it again.'];
  }
  if (state === 'override-above-org') {
    return ['an override above the organization value has no effect: organization '
      + 'limits always apply. Remove it so the configuration says what is '
      + 'actually enforced.'];
  }
  if (state === 'project-outlier') {
    return ['raise it with the admin update call at /v1/organization/projects/'
      + '{project_id}/rate_limits/{rate_limit_id}, sending the dimension you want '
      + 'changed. That is a write and this script does not make it.',
      'the peer maximum is a proxy for the tier value, not the tier value: this '
      + 'object carries no organization number. Confirm against the tier before '
      + 'treating the gap as the whole story.'];
  }
  if (state === 'org-limit-unknown') {
    return ['the organization publishes no number for this limiter, so the '
      + 'override is unjudgeable rather than fine. Read '
      + '/v1/organizations/rate_limits for the group before acting.'];
  }
  return [];
}

async function read(url, headers, params) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) target.searchParams.set(k, String(v));
  const r = await fetch(target, { headers });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from ${url}: this path needs an organization `
                    + 'scoped read credential, not a workspace or project key');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function anthropicPages(headers, path, params) {
  const out = [];
  const q = { ...(params ?? {}) };
  for (let i = 0; i < 50; i += 1) {
    const page = await read(ANTHROPIC + path, headers, q);
    out.push(page);
    if (!page.next_page) break;
    q.page = page.next_page;
  }
  return out;
}

async function cursor(base, headers, path, params, cursorKey) {
  const out = [];
  const q = { ...(params ?? {}) };
  for (let i = 0; i < 50; i += 1) {
    const page = await read(base + path, headers, q);
    const data = page.data ?? [];
    out.push(...data);
    if (!page.has_more || data.length === 0) break;
    q[cursorKey] = page.last_id ?? data[data.length - 1]?.id;
  }
  return out;
}

async function auditAnthropic(key, floor) {
  const headers = { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION,
                    'User-Agent': 'rate-limit-below-org-audit/1.0' };
  const org = orgIndex(await anthropicPages(headers, '/organizations/rate_limits'));
  const spaces = await cursor(ANTHROPIC, headers, '/organizations/workspaces',
                              { limit: 100 }, 'after_id');
  console.log(`anthropic: ${spaces.length} workspace(s), ${Object.keys(org).length} `
              + 'organization rate limit group(s)');

  let findings = 0;
  for (const space of spaces) {
    const wid = space?.id ?? '?';
    const name = space?.name ?? '(unnamed)';
    const pages = await anthropicPages(
      headers, `/organizations/workspaces/${wid}/rate_limits`);
    const entries = pages.flatMap((p) => p.data ?? []);
    if (entries.length === 0) {
      console.log(`${'no-override'.padEnd(22)} ${wid} ${name}: inherits every `
                  + 'organization limit');
      continue;
    }
    for (const entry of entries) {
      const label = groupLabel(entry);
      const orgTypes = org[label] ?? {};
      const states = [];
      const rows = [];
      for (const [ltype, value, orgLimit] of overridesOf(entry)) {
        const fallback = orgLimit === null ? (orgTypes[ltype] ?? null) : orgLimit;
        const [state, detail] = gradeOverride(value, fallback, floor);
        states.push(state);
        rows.push([ltype, detail]);
      }
      const inherited = inheritedLimiters(entry, orgTypes);
      if (inherited.length && states.length) states.push('limiter-inherited');
      const state = verdict(states);
      console.log(`${state.padEnd(22)} ${wid} ${name} / ${label}`);
      for (const [ltype, detail] of rows) console.log(`  ${ltype.padEnd(26)} ${detail}`);
      for (const [ltype, value] of inherited) {
        console.log(`  inherited: ${ltype} (${fmt(value)} from the organization)`);
      }
      for (const line of repairLines(state)) console.log(`  repair: ${line}`);
      if (FINDINGS.has(state)) findings += 1;
    }
  }
  return findings;
}

async function auditOpenai(key, floor) {
  const headers = { Authorization: `Bearer ${key}`,
                    'User-Agent': 'rate-limit-below-org-audit/1.0' };
  const projects = await cursor(OPENAI, headers, '/organization/projects',
                                { limit: 100, include_archived: 'false' }, 'after');
  const byProject = {};
  const names = {};
  for (const project of projects) {
    const pid = project?.id ?? '?';
    names[pid] = project?.name ?? '(unnamed)';
    byProject[pid] = await cursor(
      OPENAI, headers, `/organization/projects/${pid}/rate_limits`, { limit: 100 }, 'after');
  }

  const matrix = openaiMatrix(byProject);
  const comparable = Object.values(matrix).filter((m) => Object.keys(m).length >= 2).length;
  console.log(`openai: ${projects.length} project(s), ${comparable} model row(s) `
              + 'carried by 2 or more projects');
  if (projects.length < 2) {
    console.log(`${'no-peer'.padEnd(22)} one project only: this object carries no `
                + 'organization value, so there is nothing to compare against');
    return 0;
  }

  const rows = openaiOutliers(matrix, floor);
  const dimension = { rpm: 'max_requests_per_1_minute', tpm: 'max_tokens_per_1_minute' };
  const seen = new Set();
  for (const [model, pid, dim, value, peerMax] of rows) {
    console.log(`${'project-outlier'.padEnd(22)} ${pid} ${names[pid] ?? '(unnamed)'}  ${model}`);
    console.log(`  ${dimension[dim].padEnd(26)} ${fmt(value)} against a peer maximum `
                + `of ${fmt(peerMax)} (${Math.round((100 * value) / peerMax)}%)`);
    seen.add(`${pid}|${model}`);
  }
  if (rows.length) {
    for (const line of repairLines('project-outlier')) console.log(`  repair: ${line}`);
  }
  return seen.size;
}

async function main() {
  const anthropicKey = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  const openaiKey = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!anthropicKey && !openaiKey) {
    console.error('set ANTHROPIC_ADMIN_KEY, OPENAI_ADMIN_KEY, or both. Each must be '
                  + 'an organization scoped read credential; a workspace or project '
                  + 'key cannot reach these paths');
    process.exitCode = 2;
    return;
  }
  const floor = Number((process.env.FLOOR || "dummy-floor") ?? 0.5);
  let findings = 0;
  if (anthropicKey) findings += await auditAnthropic(anthropicKey, floor);
  if (openaiKey) findings += await auditOpenai(openaiKey, floor);
  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
