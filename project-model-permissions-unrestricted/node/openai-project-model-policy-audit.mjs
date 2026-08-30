/**
 * Find OpenAI projects whose model permission policy excludes nothing.
 *
 * Read only. One paged GET for the project list, two GETs per project for the
 * permission objects, and five usage reads. No request body is constructed;
 * the least-privilege policy is printed as text.
 *
 * The script has no opinion about which model suits which workload. It asks
 * whether a policy exists and whether it has ever excluded anything.
 */
const API = 'https://api.openai.com/v1';
const DAY = 86400;

const TOOL_USAGE = {
  web_search: ['/organization/usage/web_search_calls', 'num_requests'],
  code_interpreter: ['/organization/usage/code_interpreter_sessions', 'num_sessions'],
  file_search: ['/organization/usage/file_search_calls', 'num_requests'],
  image_generation: ['/organization/usage/images', 'num_model_requests'],
};

const FINDINGS = new Set(['no-policy', 'deny-list-empty', 'allow-list-empty',
                          'deny-list-fails-open', 'allow-list-wider-than-use',
                          'policy-unreadable']);

const SEVERITY = { 'deny-list-empty': 0, 'no-policy': 1,
                   'allow-list-wider-than-use': 2, 'deny-list-fails-open': 3,
                   'allow-list-empty': 4, 'policy-unreadable': 5 };

/** The non-empty model ids on a policy. Pure. */
export function policyIds(policy) {
  return (policy?.model_ids ?? [])
    .map((v) => String(v ?? '').trim())
    .filter(Boolean);
}

/** Shape of one model permissions object. Pure. */
export function policyState(policy) {
  if (policy === null || policy === undefined) return 'absent';
  const mode = String(policy?.mode ?? '').trim().toLowerCase();
  const ids = policyIds(policy);
  if (mode === 'deny_list') return ids.length ? 'deny-list' : 'deny-empty';
  if (mode === 'allow_list') return ids.length ? 'allow-list' : 'allow-empty';
  return 'unreadable';
}

/** Does this policy permit every model? Pure. Narrow on purpose. */
export function unrestricted(policy) {
  const shape = policyState(policy);
  return shape === 'absent' || shape === 'deny-empty';
}

/** {project_id: {model: requests}} across usage buckets. Pure. */
export function foldModels(buckets, countField = 'num_model_requests') {
  const out = {};
  for (const bucket of buckets ?? []) {
    for (const result of bucket?.results ?? []) {
      const n = Math.trunc(Number(result?.[countField] ?? 0));
      if (!Number.isFinite(n) || n <= 0) continue;
      const pid = String(result?.project_id ?? 'unattributed');
      const model = String(result?.model ?? 'unknown');
      const entry = (out[pid] ??= {});
      entry[model] = (entry[model] ?? 0) + n;
    }
  }
  return out;
}

/** Allow-listed models that served nothing in the window. Pure. */
export function unusedAllowed(policy, used) {
  if (policyState(policy) !== 'allow-list') return [];
  const seen = new Set(Object.keys(used ?? {}));
  return policyIds(policy).filter((m) => !seen.has(m)).sort();
}

/** [[tool, why]] for enabled hosted tools. Pure. */
export function unusedTools(perms, counts) {
  const out = [];
  for (const tool of Object.keys(perms ?? {}).sort()) {
    const block = perms[tool];
    if (!block || typeof block !== 'object' || !block.enabled) continue;
    if (!(tool in TOOL_USAGE)) {
      out.push([tool, 'enabled, and no usage endpoint counts it']);
      continue;
    }
    if (Math.trunc(Number((counts ?? {})[tool] ?? 0)) <= 0) {
      const name = TOOL_USAGE[tool][0].split('/').pop();
      out.push([tool, `enabled, and ${name} reports nothing in the window`]);
    }
  }
  return out;
}

/** Classify one project's model policy. Pure. Returns [state, detail]. */
export function classify(policy, used, days = 30) {
  const shape = policyState(policy);
  const seen = Object.keys(used ?? {}).sort();

  if (shape === 'absent') {
    return ['no-policy',
            'no model permissions policy is configured; every model the '
            + 'organization is entitled to is reachable from this project'];
  }
  if (shape === 'unreadable') {
    return ['policy-unreadable',
            'the policy object has no recognisable mode and will not be graded '
            + 'as restrictive'];
  }
  if (shape === 'deny-empty') {
    return ['deny-list-empty',
            'a policy object exists, mode is deny_list, and model_ids is empty. '
            + 'This permits every model and looks configured'];
  }
  if (shape === 'allow-empty') {
    return ['allow-list-empty',
            'mode is allow_list with no model_ids, which permits nothing. If this '
            + 'project is serving traffic, something else is going on'];
  }
  if (shape === 'deny-list') {
    return ['deny-list-fails-open',
            `deny_list naming ${policyIds(policy).length} model(s). Restrictive `
            + 'today and open by construction to anything released tomorrow'];
  }
  const spare = unusedAllowed(policy, used);
  if (spare.length) {
    return ['allow-list-wider-than-use',
            `allow_list names ${policyIds(policy).length} model(s); ${seen.length} `
            + `served any request in the last ${days} day(s). Unused: ${spare.join(', ')}`];
  }
  return ['restricted',
          `allow_list of ${policyIds(policy).length} model(s), all of them in use`];
}

/** The repair for one project. Pure. Printed, never performed. */
export function repairLines(state, projectId, used) {
  const lines = [];
  if (!FINDINGS.has(state)) return lines;
  if (state === 'no-policy') {
    lines.push('add the policy call to whatever creates projects. It does not '
      + 'inherit from the organization or from any other project.');
  } else if (state === 'deny-list-empty') {
    lines.push('somebody opened this policy and did not finish it. Find out who, '
      + 'and whether anything downstream assumed it was done.');
  } else if (state === 'deny-list-fails-open') {
    lines.push('a deny list permits every model that does not exist yet. Switch to '
      + 'an allow list unless keeping one named model out is genuinely the whole '
      + 'requirement.');
  } else if (state === 'allow-list-empty') {
    lines.push('this permits nothing. Read it before changing it; an empty allow '
      + 'list is more often a mistake than a lockdown.');
  } else if (state === 'policy-unreadable') {
    lines.push('read the policy object by hand. This audit will not call an '
      + 'unrecognised mode restrictive.');
  }
  const observed = Object.keys(used ?? {}).sort();
  if (observed.length) {
    lines.push(`POST /v1/organization/projects/${projectId}/model_permissions with `
      + `{"mode": "allow_list", "model_ids": ${JSON.stringify(observed)}}`);
    lines.push('that list is what this project already called in the window. It is '
      + 'a starting point, not a recommendation about which model suits the work.');
  } else {
    lines.push('this project called no model in the window, so there is no observed '
      + 'set to build an allow list from. Decide it deliberately rather than '
      + 'copying another project.');
  }
  return lines;
}

async function read(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) for (const one of v) url.searchParams.append(k, String(one));
    else url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/organization/* needs an `
      + 'organization admin key, not a project key');
  }
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function paged(key, path, params) {
  const out = [];
  const q = { ...params };
  for (;;) {
    const page = (await read(key, path, q)) ?? {};
    const data = page.data ?? [];
    out.push(...data);
    if (!page.has_more || data.length === 0) return out;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function usage(key, path, start, end) {
  const params = { start_time: start, end_time: end, bucket_width: '1d',
                   limit: 31, group_by: ['project_id', 'model'] };
  const out = [];
  for (;;) {
    const page = (await read(key, path, params)) ?? {};
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) return out;
    params.page = page.next_page;
  }
}

async function toolCounts(key, start, end) {
  const out = {};
  for (const [tool, [path, field]] of Object.entries(TOOL_USAGE)) {
    const page = (await read(key, path, { start_time: start, end_time: end,
      bucket_width: '1d', limit: 31, group_by: ['project_id'] })) ?? {};
    for (const bucket of page.data ?? []) {
      for (const result of bucket?.results ?? []) {
        const n = Math.trunc(Number(result?.[field] ?? 0)) || 0;
        const pid = String(result?.project_id ?? 'unattributed');
        const entry = (out[pid] ??= {});
        entry[tool] = (entry[tool] ?? 0) + n;
      }
    }
  }
  return out;
}

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key; a project '
                  + 'key cannot read the per-project permission endpoints');
    process.exitCode = 2;
    return;
  }
  const days = Number((process.env.DAYS || "dummy-days") ?? 30);
  const end = Math.floor(Date.now() / 1000);
  const start = end - Math.max(1, days) * DAY;

  const used = foldModels(await usage(admin, '/organization/usage/completions', start, end));
  const counts = await toolCounts(admin, start, end);
  const projects = await paged(admin, '/organization/projects', { limit: 100 });

  const findings = [];
  const toolFindings = [];
  for (const project of projects) {
    const pid = String(project.id ?? '');
    const policy = await read(admin, `/organization/projects/${pid}/model_permissions`);
    const [state, detail] = classify(policy, used[pid], days);
    if (FINDINGS.has(state)) findings.push([project, state, detail]);
    const perms = (await read(admin,
      `/organization/projects/${pid}/hosted_tool_permissions`)) ?? {};
    const spare = unusedTools(perms, counts[pid]);
    if (spare.length) toolFindings.push([project, spare]);
  }

  console.log(`${projects.length} project(s), ${findings.length} policy finding(s), `
              + `${toolFindings.length} project(s) with unused hosted tools`);

  findings.sort(([pa, sa], [pb, sb]) =>
    (SEVERITY[sa] ?? 9) - (SEVERITY[sb] ?? 9)
    || String(pa.name ?? '').localeCompare(String(pb.name ?? '')));

  for (const [project, state, detail] of findings) {
    const pid = String(project.id ?? '');
    console.warn(`${state.padEnd(26)} ${pid.padEnd(14)} ${project.name ?? '(unnamed)'}`);
    console.warn(`  ${detail}`);
    for (const line of repairLines(state, pid, used[pid])) {
      console.warn(`  repair: ${line}`);
    }
  }
  for (const [project, spare] of toolFindings) {
    console.warn(`${'hosted tools'.padEnd(26)} ${String(project.id).padEnd(14)} `
                 + `${project.name ?? '(unnamed)'}`);
    for (const [tool, why] of spare) console.warn(`  ${tool}: ${why}`);
  }
  process.exitCode = (findings.length || toolFindings.length) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
