/**
 * Find billed, salvageable output left behind by cancelled batches.
 *
 * Read only, on both providers: /v1/batches on OpenAI and /v1/messages/batches
 * on Anthropic. Nothing is cancelled, submitted or downloaded.
 *
 * Cancel is a stop, not a rollback. Anthropic documents that canceled and
 * expired requests are not billed; OpenAI documents the partial output but not
 * the billing split, so its completed count is reported as a floor.
 */
const OPENAI_BATCHES_URL = 'https://api.openai.com/v1/batches';
const ANTHROPIC_BATCHES_URL = 'https://api.anthropic.com/v1/messages/batches';

export const STUCK_SECONDS = 15 * 60;

const OPENAI_CANCEL_STATES = new Set(['cancelling', 'cancelled']);

const FINDINGS = new Set(['cancel-stuck', 'cancel-partial-unclaimed']);

/** Epoch seconds from a unix number or an RFC 3339 string. Pure. */
export function parseTime(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
    return null;
  }
  if (typeof value === 'number') return Math.trunc(value);
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

/** Normalised rows for OpenAI batches under cancellation. Pure. */
export function openaiCancelRows(batches) {
  return (batches ?? [])
    .filter((b) => OPENAI_CANCEL_STATES.has((b ?? {}).status))
    .map((b) => {
      const counts = b.request_counts ?? {};
      const total = Number(counts.total) || 0;
      const done = Number(counts.completed) || 0;
      const failed = Number(counts.failed) || 0;
      return {
        provider: 'openai',
        id: String(b.id),
        status: b.status,
        inFlight: b.status === 'cancelling',
        done,
        stopped: Math.max(0, total - done - failed),
        total,
        artifact: b.output_file_id ?? null,
        cancelStarted: parseTime(b.cancelling_at),
        billingKnown: false,
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** Normalised rows for Claude batches under cancellation. Pure. */
export function anthropicCancelRows(batches) {
  return (batches ?? [])
    .filter((b) => (b ?? {}).cancel_initiated_at)
    .map((b) => {
      const counts = b.request_counts ?? {};
      const done = Number(counts.succeeded) || 0;
      const stopped = Number(counts.canceled) || 0;
      const status = String(b.processing_status ?? '') || 'unknown';
      return {
        provider: 'anthropic',
        id: String(b.id),
        status,
        inFlight: status === 'canceling',
        done,
        stopped,
        total: done + stopped + (Number(counts.errored) || 0)
               + (Number(counts.expired) || 0) + (Number(counts.processing) || 0),
        artifact: b.results_url ?? null,
        cancelStarted: parseTime(b.cancel_initiated_at),
        billingKnown: true,
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** Rows holding finished work a re-run would pay for again. Pure. */
export function salvageRows(rows) {
  return (rows ?? []).filter((r) => (Number(r?.done) || 0) > 0);
}

/** Rows still mid cancel past the threshold. Pure. now is an argument. */
export function stuckRows(rows, now, seconds = STUCK_SECONDS) {
  return (rows ?? []).filter((r) => {
    if (!r?.inFlight) return false;
    const started = r.cancelStarted;
    return started === null || started === undefined || now - started > seconds;
  });
}

/** Finished rows across everything cancelled. Pure. */
export function salvagedTotal(rows) {
  return salvageRows(rows).reduce((n, r) => n + (Number(r.done) || 0), 0);
}

/** Grade the run. Pure. Returns [state, detail]. */
export function verdict(rows, stuck, salvage) {
  const all = rows ?? [];
  const s = stuck ?? [];
  const v = salvage ?? [];
  if (!all.length) {
    return ['no-cancels',
      'no batch on the providers checked has had a cancellation initiated'];
  }
  if (s.length) {
    let detail = `${s.length} batch(es) have been mid cancel longer than the `
      + 'documented window';
    if (v.length) {
      detail += `, and ${v.length} cancelled batch(es) hold ${salvagedTotal(v)} `
        + 'finished rows nothing has collected';
    }
    return ['cancel-stuck', detail];
  }
  if (v.length) {
    return ['cancel-partial-unclaimed',
      `${v.length} cancelled batch(es) hold ${salvagedTotal(v)} finished rows a `
      + 're-run would pay for again'];
  }
  return ['cancel-clean',
    `${all.length} cancellation(s) found, none of which had completed a single `
    + 'request, so there is nothing to salvage and nothing to double pay'];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, rows) {
  const all = rows ?? [];
  if (state === 'no-cancels') return [];
  if (state === 'cancel-clean') {
    return ['nothing to collect. Keep cancelling early: a batch stopped before '
      + 'its first request completed costs nothing.'];
  }
  const lines = [];
  if (state === 'cancel-stuck') {
    lines.push('a batch still in cancelling or canceling has not stopped. Poll '
      + 'it to a terminal state before you submit a replacement, or the two '
      + 'will run the same rows at once.');
  }
  if (all.some((r) => (Number(r?.done) || 0) > 0)) {
    lines.push('download the partial output, drop those custom_ids from the '
      + 'input file, and submit only the remainder. Results are not returned in '
      + 'request order, so custom_id is the only join key that works.');
  }
  if (all.some((r) => r?.provider === 'anthropic' && (Number(r.done) || 0) > 0)) {
    lines.push('on Anthropic, canceled and expired requests are not billed, so '
      + 'the succeeded count is the whole cost of the cancelled batch.');
  }
  if (all.some((r) => r?.provider === 'openai' && (Number(r.done) || 0) > 0)) {
    lines.push('on OpenAI the billing split for a cancelled batch is not '
      + 'documented, so treat the completed count as a floor and confirm the '
      + 'day against the cost report.');
  }
  return lines;
}

async function getJson(url, headers, params) {
  const target = new URL(url);
  for (const [k, v] of Object.entries(params ?? {})) target.searchParams.set(k, String(v));
  let res;
  try {
    res = await fetch(target, { headers });
  } catch (err) {
    return [null, `request failed: ${err.message}`];
  }
  if (res.status !== 200) return [null, `HTTP ${res.status}`];
  try {
    return [await res.json(), null];
  } catch {
    return [null, 'response was not JSON'];
  }
}

async function pageOpenai(key, maxPages) {
  const rows = [];
  let after = null;
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const params = { limit: 100 };
    if (after) params.after = after;
    const [payload, err] = await getJson(OPENAI_BATCHES_URL,
      { Authorization: `Bearer ${key}` }, params);
    if (err) return [rows, err];
    const data = payload.data ?? [];
    rows.push(...data);
    if (!payload.has_more || !data.length) break;
    after = data[data.length - 1]?.id;
  }
  return [rows, null];
}

async function pageAnthropic(key, maxPages) {
  const rows = [];
  let after = null;
  for (let i = 0; i < Math.max(1, maxPages); i += 1) {
    const params = { limit: 1000 };
    if (after) params.after_id = after;
    const [payload, err] = await getJson(ANTHROPIC_BATCHES_URL,
      { 'x-api-key': key, 'anthropic-version': '2023-06-01' }, params);
    if (err) return [rows, err];
    const data = payload.data ?? [];
    rows.push(...data);
    if (!payload.has_more || !data.length) break;
    after = payload.last_id ?? data[data.length - 1]?.id;
  }
  return [rows, null];
}

function args(argv) {
  const out = { stuckMinutes: 15, maxPages: 20 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--stuck-minutes') out.stuckMinutes = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--max-pages') out.maxPages = Number.parseInt(argv[i += 1], 10);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const openaiKey = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const anthropicKey = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!openaiKey && !anthropicKey) {
    console.error('set OPENAI_API_KEY (project key, Read Only) or '
      + 'ANTHROPIC_API_KEY (workspace key), or both');
    process.exitCode = 2;
    return;
  }

  const now = Math.floor(Date.now() / 1000);
  const rows = [];
  const checked = [];
  if (openaiKey) {
    checked.push('openai');
    const [batches, err] = await pageOpenai(openaiKey, opts.maxPages);
    if (err) console.log(`openai batch list stopped early: ${err}`);
    rows.push(...openaiCancelRows(batches));
  }
  if (anthropicKey) {
    checked.push('anthropic');
    const [batches, err] = await pageAnthropic(anthropicKey, opts.maxPages);
    if (err) console.log(`anthropic batch list stopped early: ${err}`);
    rows.push(...anthropicCancelRows(batches));
  }

  const stuck = stuckRows(rows, now, Math.max(1, opts.stuckMinutes) * 60);
  const salvage = salvageRows(rows);
  const stuckIds = new Set(stuck.map((r) => r.id));

  for (const r of rows) {
    console.log(`${r.provider.padEnd(11)} ${r.id.slice(0, 14).padEnd(14)} `
      + `${r.status.padEnd(11)} ${r.done} of ${r.total} done, ${r.stopped} stopped`);
    if (r.artifact) {
      const label = r.provider === 'openai' ? 'output_file_id' : 'results_url';
      console.log(`${''.padEnd(27)} ${label} present`);
    }
    if (stuckIds.has(r.id)) {
      const age = r.cancelStarted === null || r.cancelStarted === undefined
        ? 'an unknown time'
        : `${Math.floor((now - r.cancelStarted) / 60)} min`;
      console.log(`${r.provider.padEnd(11)} ${r.id.slice(0, 14).padEnd(14)}   `
        + `mid cancel for ${age}`);
    }
  }

  const [state, detail] = verdict(rows, stuck, salvage);
  console.log(`${state.padEnd(20)} ${detail}`);
  console.log(`  checked: ${checked.join(', ')}`);
  console.log('  measured: request_counts and the cancellation timestamps from '
    + 'the batch lists');
  console.log('  inferred: that a re-run would repeat the finished rows, since '
    + 'neither API records whether the partial output was downloaded');
  for (const line of repairLines(state, rows)) console.log(`  repair: ${line}`);

  const total = stuck.length + salvage.length;
  console.log(`${total} finding(s)`);
  process.exitCode = total ? 1 : 0;
  void FINDINGS;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
