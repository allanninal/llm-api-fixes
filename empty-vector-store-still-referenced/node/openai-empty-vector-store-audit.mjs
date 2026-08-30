/**
 * Check that the vector store ids your application configures index anything.
 *
 * Read only. One GET per configured id plus a paged listing. No request body,
 * and no file_search query is ever run: a retrieval query is a generation, and
 * the question here is whether the index is empty rather than what it answers.
 *
 * The configured ids are the input. An empty vector store is an ordinary
 * object; it becomes a fault only when something still names it.
 */
const API = 'https://api.openai.com/v1';
const BETA = { 'OpenAI-Beta': 'assistants=v2' };

const FINDINGS = new Set(['referenced-empty', 'referenced-nothing-indexed',
                          'referenced-zero-bytes', 'referenced-missing']);

export const CAUSES = {
  expired:
    'the store passed its expiration policy and deleted its own files. That is '
    + 'the expiry note, and it will happen again on the same schedule.',
  'attach-failed':
    'files were attached and none of them indexed. That is the attach failure '
    + 'note: bucket the children by last_error.code and repair per bucket, not '
    + 'per store.',
  'still-ingesting':
    'files are still processing. You are early rather than broken; re-read once '
    + 'file_counts.in_progress is zero.',
  'never-ingested':
    'the ingest never ran against this store. Nothing was ever attached to it.',
};

/** The store ids the application claims to use. Pure. Order kept, dupes dropped. */
export function configuredIds(...raw) {
  const out = [];
  const seen = new Set();
  for (const chunk of raw) {
    if (!chunk) continue;
    const items = Array.isArray(chunk) ? chunk : [chunk];
    for (const item of items) {
      for (const token of String(item ?? '').trim().split(/[,\s]+/)) {
        if (token && !seen.has(token)) { seen.add(token); out.push(token); }
      }
    }
  }
  return out;
}

/** The five file_counts integers, coerced. Pure. */
export function counts(store) {
  const raw = store?.file_counts ?? {};
  const out = {};
  for (const key of ['in_progress', 'completed', 'failed', 'cancelled', 'total']) {
    const n = Number(raw[key] ?? 0);
    out[key] = Number.isFinite(n) ? Math.trunc(n) : 0;
  }
  return out;
}

/** usage_bytes as an integer. Pure. Missing or unparseable reads as 0. */
export function usageBytes(store) {
  const n = Number(store?.usage_bytes ?? 0);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** How empty one store is. Pure. Four words, tested in a load-bearing order. */
export function emptiness(store) {
  const c = counts(store);
  if (c.total <= 0) return 'no-files';
  if (c.completed <= 0) return 'nothing-completed';
  if (usageBytes(store) <= 0) return 'zero-bytes';
  return 'indexed';
}

/** Why the store is empty, as far as the object can say. Pure. */
export function cause(store) {
  if (String(store?.status ?? '').trim().toLowerCase() === 'expired') return 'expired';
  const c = counts(store);
  if (c.failed > 0) return 'attach-failed';
  if (c.in_progress > 0) return 'still-ingesting';
  return 'never-ingested';
}

/** Grade one store. Pure. Returns [state, detail]. */
export function classify(store, referenced) {
  if (store === null || store === undefined) {
    if (referenced) {
      return ['referenced-missing',
              'no such store for this key. Vector stores are project scoped, so '
              + 'the usual cause is a key from the wrong project rather than a '
              + 'deleted store.'];
    }
    return ['not-found', 'no such store'];
  }

  const c = counts(store);
  const kind = emptiness(store);
  const size = usageBytes(store);

  if (!referenced) {
    if (kind === 'indexed') {
      return ['unreferenced',
              `${c.completed} file(s) completed, and nothing you passed names it`];
    }
    return ['abandoned-empty',
            'empty and unreferenced, which is litter rather than an outage'];
  }

  if (kind === 'no-files') return ['referenced-empty', '0 file(s) attached, 0 bytes'];
  if (kind === 'nothing-completed') {
    return ['referenced-nothing-indexed',
            `${c.total} attached, 0 completed, ${c.failed} failed, `
            + `${c.in_progress} in progress`];
  }
  if (kind === 'zero-bytes') {
    return ['referenced-zero-bytes',
            `${c.completed} file(s) report completed and usage_bytes is 0, which `
            + 'the three emptiness tests disagree about. Read it before acting.'];
  }
  return ['grounded',
          `${c.completed} file(s) completed, ${(size / 1048576).toFixed(1)} MiB`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, why = null) {
  const assertion = 'assert file_counts.completed > 0 for every id in '
    + 'vector_store_ids at startup and refuse to boot. A retrieval feature that '
    + 'cannot retrieve should fail at deploy, not in an answer.';
  if (state === 'referenced-empty') {
    const lines = [];
    if (why === 'expired') lines.push(CAUSES.expired);
    lines.push('run the ingest, then re-read the store before shipping the id.');
    lines.push(assertion);
    return lines;
  }
  if (state === 'referenced-nothing-indexed') {
    return [CAUSES[why] ?? CAUSES['attach-failed'], assertion];
  }
  if (state === 'referenced-zero-bytes') {
    return ['do not delete this one on the strength of a byte count. Read the '
            + 'store and one of its files before deciding what it is.', assertion];
  }
  if (state === 'referenced-missing') {
    return ['check the project first. A project key cannot see a store that '
            + 'lives in another project, and that 404 is identical to the one a '
            + 'deleted store returns.',
            'if the store really is gone, re-ingest and update the configured id '
            + 'in the same change.',
            assertion];
  }
  if (state === 'abandoned-empty') {
    return ['nothing references it and it holds no bytes, so it is not costing '
            + 'you anything. Delete it when convenient with '
            + 'DELETE /v1/vector_stores/{vector_store_id}.'];
  }
  return [];
}

async function read(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}`, ...BETA } });
  if (r.status === 401 || r.status === 403) {
    throw new Error(`${r.status} from OpenAI: /v1/vector_stores needs a project key`);
  }
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function* paged(key, path, params, maxPages = 200) {
  const q = { ...params };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await read(key, path, q);
    const data = page?.data ?? [];
    for (const item of data) yield item;
    if (!page?.has_more || data.length === 0) return;
    q.after = page.last_id ?? data[data.length - 1]?.id;
  }
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key for the project that owns '
                  + 'the vector stores');
    process.exitCode = 2;
    return;
  }
  const wanted = configuredIds((process.env.VECTOR_STORE_IDS || "dummy-vector-store-ids"));
  if (!wanted.length) {
    console.error('pass the store ids your application configures as '
                  + 'VECTOR_STORE_IDS. Without them this script has nothing to '
                  + 'grade: an empty store is only a finding when something '
                  + 'still names it.');
    process.exitCode = 2;
    return;
  }

  const stores = [];
  for await (const st of paged(key, '/vector_stores', { limit: 100 })) stores.push(st);
  const byId = new Map(stores.map((st) => [st?.id, st]));
  console.log(`${wanted.length} configured id(s), ${stores.length} store(s) `
              + 'visible to this key');

  let findings = 0;
  for (const sid of wanted) {
    const store = byId.get(sid) ?? await read(key, `/vector_stores/${sid}`);
    const [state, detail] = classify(store, true);
    const why = store ? cause(store) : null;
    console.log(`${state.padEnd(26)} ${sid} ${store?.name ?? ''}: ${detail}`);
    if (FINDINGS.has(state) && store) console.log(`  cause: ${CAUSES[why]}`);
    for (const line of repairLines(state, why)) console.log(`  repair: ${line}`);
    if (FINDINGS.has(state)) findings += 1;
  }

  const configured = new Set(wanted);
  const litter = stores.filter((st) => !configured.has(st?.id)
                                       && emptiness(st) !== 'indexed');
  if (litter.length) {
    console.log(`${'abandoned-empty'.padEnd(26)} ${litter.length} empty store(s) `
                + 'nothing references, which is litter');
    for (const line of repairLines('abandoned-empty')) console.log(`  note: ${line}`);
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
