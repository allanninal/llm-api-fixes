/**
 * Estimate whether a non-streaming Claude call can finish inside 10 minutes.
 *
 * Read only, with one deliberate exception. Nothing here creates a completion:
 * where a call path names a payload file, that body goes to
 * /v1/messages/count_tokens, which is free, creates no object, generates no
 * output and is not billed. It is used to turn the input into prefill seconds.
 * Everything else is a GET, and /v1/messages is never called.
 *
 * The repair is a transport change and it is printed.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const WALL_CLOCK = 600;
const DEFAULT_TPS = 55;
const DEFAULT_PREFILL_TPS = 6000;

const SDK_TIMEOUT_UNITS = {
  python: ['seconds', 1],
  ruby: ['seconds', 1],
  php: ['seconds', 1],
  typescript: ['milliseconds', 0.001],
  javascript: ['milliseconds', 0.001],
  node: ['milliseconds', 0.001],
  go: ['a time.Duration', 1],
  java: ['a Duration', 1],
  csharp: ['a TimeSpan', 1],
};
const MILLISECOND_SDKS = new Set(['typescript', 'javascript', 'node']);

const SAMPLING_ONLY = new Set(['max_tokens', 'stream', 'temperature', 'top_p',
  'top_k', 'stop_sequences', 'metadata', 'service_tier']);

const FINDINGS = new Set(['over-wall-clock-not-streaming', 'over-client-timeout',
  'near-wall-clock-not-streaming']);

/** Seconds as minutes and seconds. Pure. */
export function duration(seconds) {
  const total = Math.trunc(Math.max(0, Number(seconds || 0)));
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
}

/** How long it takes to write maxTokens output tokens. Pure. */
export function generationSeconds(maxTokens, tps = DEFAULT_TPS) {
  const rate = Number(tps || 0);
  if (rate <= 0) return 0;
  return Math.max(0, Math.trunc(maxTokens || 0)) / rate;
}

/** How long it takes to read the input. Pure. Reported separately on purpose. */
export function prefillSeconds(inputTokens, prefillTps = DEFAULT_PREFILL_TPS) {
  const rate = Number(prefillTps || 0);
  if (rate <= 0) return 0;
  return Math.max(0, Math.trunc(inputTokens || 0)) / rate;
}

/**
 * A client timeout in seconds, whatever unit the SDK takes. Pure.
 * Null when the SDK is unknown, because guessing the unit is the mistake this
 * function exists to catch.
 */
export function timeoutSeconds(sdk, value) {
  if (value === null || value === undefined) return null;
  const unit = SDK_TIMEOUT_UNITS[String(sdk ?? '').trim().toLowerCase()];
  if (!unit) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n * unit[1] : null;
}

/**
 * True when a timeout looks written in the wrong unit. Pure.
 * 600 in the TypeScript client is six hundred milliseconds, not ten minutes.
 */
export function unitSuspicion(sdk, value) {
  const seconds = timeoutSeconds(sdk, value);
  if (seconds === null) return false;
  return MILLISECOND_SDKS.has(String(sdk ?? '').trim().toLowerCase()) && seconds < 1;
}

/** The largest max_tokens that still finishes inside the ceiling. Pure. */
export function safeMaxTokens(tps = DEFAULT_TPS, wallClock = WALL_CLOCK, prefill = 0) {
  const rate = Number(tps || 0);
  const room = Math.max(0, Number(wallClock || 0) - Math.max(0, Number(prefill || 0)));
  if (rate <= 0) return 0;
  return Math.trunc(room * rate);
}

/**
 * Classify one call path against the clock. Pure. [state, detail].
 * The wall clock is checked before the client timeout, because a non-streaming
 * request past ten minutes fails on the far side whatever the client waits for.
 */
export function verdict(seconds, streams, timeoutS = null, wallClock = WALL_CLOCK, near = 0.8) {
  const shape = `${duration(seconds)} of generation estimated`;

  if (!streams && seconds > wallClock) {
    return ['over-wall-clock-not-streaming',
      `${shape} on a non-streaming path, past the ${duration(wallClock)} ` +
      'ceiling. That is a 504 timeout_error, or no response at all when an ' +
      'intermediate hop drops the idle connection first. Raising the client ' +
      'timeout does not move it.'];
  }
  if (timeoutS !== null && timeoutS !== undefined && seconds > timeoutS) {
    return ['over-client-timeout',
      `${shape} against a client timeout of ${duration(timeoutS)}, so the ` +
      'client gives up before the API is finished.'];
  }
  if (!streams && seconds >= wallClock * near) {
    return ['near-wall-clock-not-streaming',
      `${shape} on a non-streaming path, inside ${(near * 100).toFixed(0)}% of ` +
      `the ${duration(wallClock)} ceiling. One unusually long answer crosses it.`];
  }
  if (streams && seconds > wallClock) {
    return ['streams-past-ten-minutes',
      `${shape}, and the path streams, so the connection never goes idle and ` +
      'the ceiling does not apply. Worth the Message Batches API if nobody is ' +
      'waiting on it.'];
  }
  return ['within-budget', `${shape}.`];
}

function headers(key) {
  return { 'x-api-key': key, 'anthropic-version': VERSION,
           'content-type': 'application/json' };
}

async function get(key, path) {
  const res = await fetch(API + path, { headers: headers(key) });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY has to be a workspace key`);
  }
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

/** The one non-GET call, and it neither creates nor bills anything. */
async function countInput(key, payloadPath) {
  const body = JSON.parse(await readFile(payloadPath, 'utf8'));
  const trimmed = Object.fromEntries(
    Object.entries(body).filter(([k]) => !SAMPLING_ONLY.has(k)));
  const res = await fetch(`${API}/messages/count_tokens`, {
    method: 'POST',  // count_tokens creates nothing and bills nothing
    headers: headers(key),
    body: JSON.stringify(trimmed),
  });
  if (!res.ok) throw new Error(`${res.status} from /messages/count_tokens`);
  return Math.trunc((await res.json())?.input_tokens ?? 0);
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key');
    process.exitCode = 2;
    return;
  }
  const configPath = (process.env.CONFIG || "dummy-config") ?? process.argv[2];
  if (!configPath) {
    console.error('set CONFIG, or pass the call-paths JSON file as an argument');
    process.exitCode = 2;
    return;
  }
  const paths = JSON.parse(await readFile(configPath, 'utf8'));
  const tps = Number((process.env.TPS || "dummy-tps") ?? DEFAULT_TPS);
  const prefillTps = Number((process.env.PREFILL_TPS || "dummy-prefill-tps") ?? DEFAULT_PREFILL_TPS);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const caps = new Map();
  let bad = 0;

  for (const name of Object.keys(paths).sort()) {
    const entry = paths[name] ?? {};
    const modelId = String(entry.model ?? '');
    const streams = Boolean(entry.stream);
    const sdk = entry.sdk;

    if (modelId && !caps.has(modelId)) {
      caps.set(modelId, (await get(key, `/models/${modelId}`)).max_tokens ?? null);
    }

    let inputTokens = Math.trunc(entry.input_tokens ?? 0);
    if (entry.payload) inputTokens = await countInput(key, entry.payload);

    const prefill = prefillSeconds(inputTokens, prefillTps);
    const seconds = prefill + generationSeconds(entry.max_tokens, tps);
    const client = timeoutSeconds(sdk, entry.timeout);

    const [state, detail] = verdict(seconds, streams, client);
    const line = `${state.padEnd(30)} ${name.padEnd(16)} ${detail}`;
    if (FINDINGS.has(state)) { bad += 1; console.warn(line); }
    else if (state === 'streams-past-ten-minutes') console.log(line);
    else if (showAll) console.log(line);

    if (unitSuspicion(sdk, entry.timeout)) {
      bad += 1;
      console.warn(`${'timeout-unit-mistake'.padEnd(30)} ${name.padEnd(16)} ` +
                   `timeout ${entry.timeout} on the ${sdk} client is ` +
                   `${(client ?? 0).toFixed(1)}s, not ${duration(entry.timeout ?? 0)}: ` +
                   'that unit is milliseconds');
    }

    if (state === 'over-wall-clock-not-streaming' || state === 'near-wall-clock-not-streaming') {
      console.warn(`  at ${tps.toFixed(0)} tok/s the largest max_tokens that ` +
                   `finishes inside the ceiling is ${safeMaxTokens(tps, WALL_CLOCK, prefill)}`);
      const cap = caps.get(modelId);
      if (cap) {
        console.warn(`  this model allows ${cap} output tokens, which is ` +
                     `${duration(generationSeconds(cap, tps))} on one call`);
      }
      console.warn('  repair: stream it. .stream() plus .finalMessage() returns the ' +
                   'identical Message object with no event handling, and the ' +
                   'connection never goes idle. For latency tolerant work use the ' +
                   'Message Batches API, which has no such clock. Printed, not applied.');
    }
  }

  console.log(`${Object.keys(paths).length} path(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
