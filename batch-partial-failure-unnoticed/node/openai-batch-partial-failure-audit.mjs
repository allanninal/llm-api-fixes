/**
 * Report OpenAI batches that read completed while rows inside them failed.
 *
 * Read only. GET requests and nothing else: give this a project key set to Read
 * Only. The repair is printed, never performed.
 */
const API = 'https://api.openai.com/v1';

// Still moving. None of these is a verdict about the rows, because the counts
// are not final until the batch stops.
const IN_FLIGHT = ['validating', 'in_progress', 'finalizing', 'cancelling'];

// Terminal, and owned by the sibling notes rather than by this script.
const OTHER_TERMINAL = ['failed', 'expired', 'cancelled'];

const FINDINGS = ['partial', 'unaccounted'];

/**
 * Read request_counts into three numbers, or null when it cannot be read. Pure.
 * A request_counts that is not an object returns null rather than three zeros,
 * because three zeros classify as an empty batch and that is a much calmer
 * finding than an unreadable one.
 */
export function countsOf(batch) {
  const counts = batch.request_counts;
  if (counts === null || typeof counts !== 'object' || Array.isArray(counts)) return null;
  const total = Number(counts.total ?? 0);
  const done = Number(counts.completed ?? 0);
  const failed = Number(counts.failed ?? 0);
  if (!Number.isFinite(total) || !Number.isFinite(done) || !Number.isFinite(failed)) {
    return null;
  }
  return [Math.trunc(total), Math.trunc(done), Math.trunc(failed)];
}

/**
 * Classify one object from GET /v1/batches. Pure. Returns [state, detail].
 * The two findings are kept apart on purpose: "partial" is rows that ran and
 * failed, which are in the error file, and "unaccounted" is rows that are in
 * neither column, which are not.
 */
export function verdict(batch) {
  const status = String(batch.status ?? '').trim().toLowerCase();

  if (IN_FLIGHT.includes(status)) {
    return ['running',
      `status is ${status}, so the counts are not final and there is nothing ` +
      'to reconcile yet'];
  }
  if (OTHER_TERMINAL.includes(status)) {
    return ['other-terminal',
      `status is ${status}. The batch did not finish running, which is a ` +
      'different problem from finishing with failures inside it.'];
  }
  if (status !== 'completed') {
    return ['unreadable',
      `status is ${JSON.stringify(status || null)}, which is not a lifecycle ` +
      'state this script recognises. Read the object by hand.'];
  }

  const numbers = countsOf(batch);
  if (numbers === null) {
    return ['unreadable',
      'the batch says completed and carries no readable request_counts, so ' +
      'nothing here can be reconciled. That is not the same as a clean batch ' +
      'and is not reported as one.'];
  }

  const [total, done, failed] = numbers;
  if (total <= 0) {
    return ['empty',
      'completed with a total of 0 request(s). The input file was empty or ' +
      'never parsed into rows.'];
  }
  if (failed > 0) {
    return ['partial',
      `${failed} of ${total} row(s) failed and the batch still reads ` +
      `completed. The output file is ${total - done} line(s) shorter than the ` +
      'input file.'];
  }
  if (done < total) {
    return ['unaccounted',
      `${total - done} of ${total} row(s) are neither completed nor failed. ` +
      'Rows in neither column were abandoned rather than attempted, which is ' +
      'what a closed completion window looks like in the counts.'];
  }
  return ['clean', `all ${total} row(s) completed`];
}

async function get(key, path, params = {}) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 401) {
    throw new Error('401 from OpenAI: the key is wrong, revoked, or belongs to ' +
                    'another project');
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function* walk(key, pageSize, maxPages) {
  let params = { limit: pageSize };
  for (let i = 0; i < maxPages; i += 1) {
    const page = await get(key, '/batches', params);
    const data = page.data ?? [];
    for (const batch of data) yield batch;
    if (!page.has_more || data.length === 0) return;
    params = { limit: pageSize, after: data[data.length - 1].id };
  }
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only)');
    process.exitCode = 2;
    return;
  }

  const pageSize = Number((process.env.LIMIT || "dummy-limit") ?? 100);
  const maxPages = Number((process.env.PAGES || "dummy-pages") ?? 20);
  const showAll = process.argv.includes('--show-all');

  let checked = 0;
  let bad = 0;
  for await (const batch of walk(key, pageSize, maxPages)) {
    const [state, detail] = verdict(batch);
    const batchId = String(batch.id ?? '?');
    const line = `${state.padEnd(15)} ${batchId}  ${detail}`;

    if (FINDINGS.includes(state)) {
      checked += 1;
      bad += 1;
      console.warn(line);
      console.warn(batch.error_file_id
        ? `  repair: read the failures with GET /v1/files/${batch.error_file_id}` +
          '/content, bucket the lines by error.code, and re-submit the failed ' +
          'custom_ids as a new batch'
        : '  repair: no error_file_id on this batch, so the missing rows were ' +
          'never attempted. Re-submit them and reconcile output lines against ' +
          'input lines.');
      console.warn('  repair: treat request_counts.failed > 0 as a job failure ' +
                   'in your orchestrator instead of trusting status == completed');
    } else if (state === 'clean') {
      checked += 1;
      if (showAll) console.log(line);
    } else if (state === 'unreadable' || state === 'empty') {
      checked += 1;
      console.warn(line);
    } else if (showAll) {
      console.log(line);
    }
  }

  console.log(`${checked} completed batch(es) checked, ${bad} with rows missing`);
  process.exitCode = bad ? 1 : 0;
}

// Only run when invoked directly. The test file imports this module, and without
// the guard main() would run there too, fail on the missing key, and set a
// non-zero exit code that fails the whole test file even as every test passes.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
