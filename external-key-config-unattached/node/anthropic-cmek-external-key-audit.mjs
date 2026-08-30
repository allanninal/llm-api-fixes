/**
 * Find Anthropic CMEK key configs that are not encrypting anything.
 *
 * Read only. Two paged GETs against /v1/organizations/external_keys and
 * /v1/organizations/workspaces with an Admin API key.
 *
 * The external keys resource offers a validate call. It is a write verb, so
 * this script does not use it. Provider coordinates are resource identifiers
 * rather than credentials, and the AWS account id inside an ARN is masked.
 */
const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const FINDINGS = new Set(['unattached-and-unused', 'unattached-but-referenced',
                          'archived-workspaces-only', 'attached-nothing-visible',
                          'geo-mismatch', 'attachment-unreadable']);

const SEVERITY = { 'unattached-and-unused': 0, 'geo-mismatch': 1,
                   'unattached-but-referenced': 2, 'attached-nothing-visible': 3,
                   'archived-workspaces-only': 4, 'attachment-unreadable': 5 };

/** "attached" / "unattached" / "unknown". Pure. Never guesses. */
export function attachmentType(key) {
  const kind = String(key?.attachment?.type ?? '').trim().toLowerCase();
  return (kind === 'attached' || kind === 'unattached') ? kind : 'unknown';
}

/** Hide the account id in an AWS ARN. Pure. Non-ARNs pass through. */
export function maskArn(arn) {
  const text = String(arn ?? '');
  const parts = text.split(':');
  if (parts.length < 6 || parts[0] !== 'arn') return text || 'unknown';
  parts[4] = '****';
  return parts.join(':');
}

/** One short line naming the KMS key. Pure. No credentials, ever. */
export function kmsRef(providerConfig) {
  const cfg = providerConfig ?? {};
  const kind = String(cfg.type ?? '').trim().toLowerCase();
  if (kind === 'aws') return `aws ${maskArn(cfg.kms_arn)}`;
  if (kind === 'gcp') return `gcp ${cfg.key_name ?? 'unknown'}`;
  if (kind === 'azure') {
    return `azure ${cfg.key_name ?? 'unknown'} in ${cfg.vault_uri ?? 'unknown vault'}`;
  }
  return `unrecognised provider ${kind || 'none'}`;
}

/** The workspace's storage geo, or null. Pure. */
export function workspaceGeo(workspace) {
  const geo = workspace?.data_residency?.workspace_geo;
  return geo ? String(geo) : null;
}

/** {external_key_id: {live: [], archived: []}}. Pure. Built from workspaces. */
export function coverage(workspaces) {
  const out = {};
  for (const workspace of workspaces ?? []) {
    const keyId = workspace?.external_key_id;
    if (!keyId) continue;
    const entry = (out[String(keyId)] ??= { live: [], archived: [] });
    entry[workspace?.archived_at ? 'archived' : 'live']
      .push(String(workspace?.id ?? 'unknown'));
  }
  for (const entry of Object.values(out)) {
    entry.live.sort();
    entry.archived.sort();
  }
  return out;
}

/** [live, archived] workspace ids with no external_key_id at all. Pure. */
export function uncovered(workspaces) {
  const live = [];
  const archived = [];
  for (const workspace of workspaces ?? []) {
    if (workspace?.external_key_id) continue;
    (workspace?.archived_at ? archived : live).push(String(workspace?.id ?? 'unknown'));
  }
  return [live.sort(), archived.sort()];
}

/** Classify one key config. Pure. Returns [state, detail]. */
export function classify(key, cover, geos) {
  const kind = attachmentType(key);
  const live = [...(cover?.live ?? [])];
  const archived = [...(cover?.archived ?? [])];

  if (kind === 'unknown') {
    return ['attachment-unreadable',
            'attachment.type is not attached or unattached, so this audit will '
            + 'not say whether the config is in use'];
  }
  if (kind === 'unattached') {
    if (live.length || archived.length) {
      return ['unattached-but-referenced',
              `the config reports unattached while ${live.length + archived.length} `
              + `workspace(s) name it (${[...live, ...archived].join(', ')}). The two `
              + 'listings disagree'];
    }
    return ['unattached-and-unused',
            'attachment.type is unattached and no workspace names it. The API '
            + 'describes this state as inert: it takes part in no encryption path'];
  }
  if (!live.length && !archived.length) {
    return ['attached-nothing-visible',
            'reported attached, and no workspace this key can enumerate names it. '
            + 'An attachment you cannot see is still an attachment'];
  }
  if (!live.length) {
    return ['archived-workspaces-only',
            `attached, and the only workspaces naming it are archived `
            + `(${archived.join(', ')}). Their retained data is still encrypted `
            + 'under this config'];
  }
  const want = String(key?.geo ?? '');
  const mismatched = (geos ?? []).filter(([, g]) => g && want && String(g) !== want);
  if (mismatched.length) {
    return ['geo-mismatch',
            `config geo is ${want} and it covers `
            + mismatched.map(([w, g]) => `${w} at ${g}`).join(', ')];
  }
  return ['covered',
          `attached, covering ${live.length} live workspace(s)`
          + (archived.length ? ` and ${archived.length} archived` : '')];
}

/** The repair for one key config. Pure. Printed, never performed. */
export function repairLines(state, key) {
  const keyId = String(key?.id ?? 'unknown');
  const lines = [];
  if (!FINDINGS.has(state)) return lines;
  if (state === 'unattached-and-unused') {
    lines.push('attach it to the workspace it was made for. Attachment is the step '
      + 'that makes a config live; creating it is not.');
    lines.push('if it was superseded, it can be deleted: DELETE '
      + `/v1/organizations/external_keys/${keyId}. Nothing depends on it.`);
  } else if (state === 'unattached-but-referenced') {
    lines.push('do not delete this. Two listings disagree, and the safe reading is '
      + 'the one that says something is using it.');
  } else if (state === 'archived-workspaces-only') {
    lines.push('do not delete this. Deleting a config an archived workspace depends '
      + "on makes that workspace's retained data unrecoverable.");
  } else if (state === 'attached-nothing-visible') {
    lines.push('the coverage map is incomplete rather than empty. Widen the '
      + 'workspace listing before concluding anything about this config.');
  } else if (state === 'geo-mismatch') {
    lines.push('a workspace cannot be re-pointed: external_key_id is write-once and '
      + 'cannot be detached or replaced. Resolve this against the residency '
      + 'commitment, not by swapping keys.');
  } else {
    lines.push('read this config by hand. The attachment discriminator was not one '
      + 'of the two values this audit recognises.');
  }
  lines.push('the validate call on this resource is a write verb and this script '
    + 'does not use it. Run it deliberately if you need it.');
  return lines;
}

async function read(key, path, params) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const r = await fetch(url, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (r.status === 401) {
    throw new Error('401 from Anthropic: /v1/organizations/* needs an Admin API '
                    + 'key, not a workspace key');
  }
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function pagedCursor(key, path, params) {
  const out = [];
  let q = { ...params };
  for (;;) {
    const page = await read(key, path, q);
    if (page === null) return out;
    out.push(...(page.data ?? []));
    if (!page.has_more || !page.next_page) return out;
    q = { ...q, page: page.next_page };
  }
}

async function pagedAfterId(key, path, params) {
  const out = [];
  const q = { ...params };
  for (;;) {
    const page = (await read(key, path, q)) ?? {};
    const data = page.data ?? [];
    out.push(...data);
    if (!page.has_more || data.length === 0) return out;
    q.after_id = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function main() {
  const admin = (process.env.ANTHROPIC_ADMIN_KEY || "dummy-anthropic-admin-key");
  if (!admin) {
    console.error('set ANTHROPIC_ADMIN_KEY to an Admin API key; a workspace key '
                  + 'cannot read /v1/organizations/*');
    process.exitCode = 2;
    return;
  }
  const wantGeo = (process.env.GEO || "dummy-geo") ?? null;

  const keys = await pagedCursor(admin, '/organizations/external_keys',
                                 { beta: 'true', limit: 100 });
  if (!keys.length) {
    const probe = await read(admin, '/organizations/external_keys',
                             { beta: 'true', limit: 1 });
    if (probe === null) {
      console.log('the external keys endpoint is not available to this '
        + 'organization. CMEK is a beta enterprise feature and this is an answer, '
        + 'not a finding.');
      return;
    }
  }

  const workspaces = await pagedAfterId(admin, '/organizations/workspaces',
    { beta: 'true', limit: 100, include_archived: 'true' });
  const cover = coverage(workspaces);
  const byId = Object.fromEntries(workspaces.map((w) => [String(w?.id), w]));

  const findings = [];
  for (const key of keys) {
    const entry = cover[String(key.id ?? '')] ?? {};
    const geos = [...(entry.live ?? []), ...(entry.archived ?? [])]
      .map((w) => [w, workspaceGeo(byId[w])]);
    const [state, detail] = classify(key, entry, geos);
    if (FINDINGS.has(state)) findings.push([key, state, detail]);
  }

  const [liveBare, archivedBare] = uncovered(workspaces);

  console.log(`${keys.length} external key config(s), ${workspaces.length} `
              + `workspace(s), ${findings.length} finding(s)`);

  findings.sort(([ka, sa], [kb, sb]) =>
    (SEVERITY[sa] ?? 9) - (SEVERITY[sb] ?? 9)
    || String(ka.id ?? '').localeCompare(String(kb.id ?? '')));

  for (const [key, state, detail] of findings) {
    console.warn(`${state.padEnd(26)} ${String(key.id).padEnd(12)} `
                 + `${key.display_name ?? '(unnamed)'}`);
    console.warn(`  ${detail}`);
    console.warn(`  provider: ${kmsRef(key.provider_config)}`);
    for (const line of repairLines(state, key)) console.warn(`  repair: ${line}`);
  }

  if (liveBare.length) {
    console.warn(`uncovered: ${liveBare.length} of ${workspaces.length} workspace(s) `
                 + `have no external_key_id at all (${liveBare.join(', ')})`);
  }
  if (archivedBare.length) {
    console.log(`uncovered and archived: ${archivedBare.length} workspace(s) `
                + `(${archivedBare.join(', ')})`);
  }
  if (wantGeo) {
    console.log(`claimed storage geo: ${wantGeo}`);
    for (const workspace of workspaces) {
      const got = workspaceGeo(workspace);
      if (got && got !== wantGeo) {
        console.warn(`residency  ${String(workspace.id).padEnd(12)} workspace_geo `
                     + `is ${got}, and ${wantGeo} was claimed`);
      }
    }
  }
  process.exitCode = (findings.length || liveBare.length) ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
