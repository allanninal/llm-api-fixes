/**
 * Find OpenAI projects whose retention posture is not the one you claim.
 *
 * Read only. One GET for the organization default, one paged GET for the
 * project list, one GET per project. No request body is constructed anywhere,
 * including for the repair, which is printed as text.
 */
const API = 'https://api.openai.com/v1';

const ZDR = new Set(['zero_data_retention', 'enhanced_zero_data_retention']);
const MAM = new Set(['modified_abuse_monitoring', 'enhanced_modified_abuse_monitoring']);
const INHERIT = 'organization_default';
const NO_CONTROL = 'none';

const FAMILY_LABEL = { zdr: 'zero data retention',
                       'modified-abuse-monitoring': 'modified abuse monitoring' };
const TARGET = { zdr: 'zero_data_retention',
                 'modified-abuse-monitoring': 'modified_abuse_monitoring' };

const FINDINGS = new Set(['retention-unreadable', 'no-retention-control',
                          'weaker-than-claimed', 'inherited-not-pinned']);
const SEVERITY = { 'no-retention-control': 0, 'weaker-than-claimed': 1,
                   'retention-unreadable': 2, 'inherited-not-pinned': 3 };

/** Group one type value into a family. Pure. Never ranks families. */
export function family(retentionType) {
  const t = String(retentionType ?? '').trim().toLowerCase();
  if (!t) return 'unreadable';
  if (ZDR.has(t)) return 'zdr';
  if (MAM.has(t)) return 'modified-abuse-monitoring';
  if (t === NO_CONTROL) return 'none';
  return 'unrecognised';
}

/** [type, inherited] for one project. Pure. */
export function effective(orgType, projectType) {
  const t = String(projectType ?? '').trim().toLowerCase();
  if (!t) return [null, false];
  if (t === INHERIT) return [String(orgType ?? '').trim().toLowerCase() || null, true];
  return [t, false];
}

/** Is this project archived? Pure. Both signals, because they disagree. */
export function archived(project) {
  return Boolean(project?.archived_at) || String(project?.status ?? '') === 'archived';
}

/** Classify one project's retention. Pure. Returns [state, detail]. */
export function classify(project, orgType, projectType, require = 'zdr') {
  const [eff, inherited] = effective(orgType, projectType);
  const fam = family(eff);
  const tail = archived(project)
    ? ' (archived, and its retained data is still retained)' : '';
  const want = FAMILY_LABEL[require] ?? require;

  if (fam === 'unreadable' || fam === 'unrecognised') {
    return ['retention-unreadable',
            `the project reports ${projectType ? `'${projectType}'` : 'nothing'}, `
            + `which this audit will not grade as safe${tail}`];
  }
  if (fam === 'none') {
    return ['no-retention-control',
            'type is none: no retention control at all, whatever the organization '
            + `default says${tail}`];
  }
  if (fam !== require) {
    return ['weaker-than-claimed',
            `resolves to ${eff} (${inherited ? 'inherited from the organization'
              : 'set on the project'})${tail}, and ${want} was claimed`];
  }
  if (inherited) {
    return ['inherited-not-pinned',
            `resolves to ${eff} only because the organization default says so. `
            + `Nothing on the project pins it${tail}`];
  }
  return ['compliant', `pinned on the project at ${eff}${tail}`];
}

/** [ok, detail] on the residency axis. Pure. Absent is unset, not GLOBAL. */
export function residencyNote(project, want) {
  if (!want) return [true, null];
  const got = project?.residency ?? null;
  if (got === null) {
    return [false, 'residency is unset on this project, which is neither GLOBAL '
                   + `nor ${want}`];
  }
  if (String(got) !== String(want)) {
    return [false, `residency is ${got}, and ${want} was claimed`];
  }
  return [true, null];
}

/** The repair for one project. Pure. Printed, never performed. */
export function repairLines(state, project, require = 'zdr') {
  const pid = String(project?.id ?? 'unknown');
  const lines = [];
  if (!FINDINGS.has(state)) return lines;
  if (state === 'inherited-not-pinned') {
    lines.push('this resolves correctly today and moves the day somebody changes '
      + 'the organization default. Pin it on the project if the commitment is '
      + 'about this workload.');
  } else if (state === 'retention-unreadable') {
    lines.push('the endpoint returned a value this audit does not recognise. Read '
      + 'it by hand before assuming anything.');
  }
  const target = TARGET[require];
  if (target) {
    lines.push(`POST /v1/organization/projects/${pid}/data_retention with a body of `
      + `{"retention_type": "${target}"}`);
    lines.push('the request field is retention_type; the response field is type. A '
      + 'body copied from the read shape 400s.');
    lines.push('zero data retention and the enhanced variants are generally enabled '
      + 'on the account by OpenAI rather than being self-serve. Request it; do not '
      + 'assume the call will take.');
  }
  return lines;
}

async function read(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
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

async function main() {
  const admin = (process.env.OPENAI_ADMIN_KEY || "dummy-openai-admin-key");
  if (!admin) {
    console.error('set OPENAI_ADMIN_KEY to an organization admin key; a project '
                  + 'key cannot read /v1/organization/data_retention');
    process.exitCode = 2;
    return;
  }
  const require_ = (process.env.REQUIRE || "dummy-require") ?? 'zdr';
  const wantResidency = (process.env.RESIDENCY || "dummy-residency") ?? null;

  const org = (await read(admin, '/organization/data_retention')) ?? {};
  console.log(`organization default: ${org.type ?? 'unreadable'}`);

  const projects = await paged(admin, '/organization/projects',
                               { limit: 100, include_archived: 'true' });
  const findings = [];
  for (const project of projects) {
    const block = (await read(admin,
      `/organization/projects/${String(project.id ?? '')}/data_retention`)) ?? {};
    const [state, detail] = classify(project, org.type, block.type, require_);
    if (FINDINGS.has(state)) findings.push([project, state, detail]);
  }

  const residencyBad = [];
  if (wantResidency) {
    for (const project of projects) {
      const [ok, detail] = residencyNote(project, wantResidency);
      if (!ok) residencyBad.push([project, detail]);
    }
  }

  console.log(`${projects.length} project(s), ${findings.length} retention `
              + `finding(s), ${residencyBad.length} residency finding(s)`);

  findings.sort(([pa, sa], [pb, sb]) =>
    (SEVERITY[sa] ?? 9) - (SEVERITY[sb] ?? 9)
    || String(pa.name ?? '').localeCompare(String(pb.name ?? '')));

  for (const [project, state, detail] of findings) {
    console.warn(`${state.padEnd(22)} ${String(project.id).padEnd(14)} `
                 + `${project.name ?? '(unnamed)'}`);
    console.warn(`  ${detail}`);
    for (const line of repairLines(state, project, require_)) {
      console.warn(`  repair: ${line}`);
    }
  }
  for (const [project, detail] of residencyBad) {
    console.warn(`${'residency'.padEnd(22)} ${String(project.id).padEnd(14)} ${detail}`);
  }
  process.exitCode = (findings.length || residencyBad.length) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
