/**
 * Find stored OpenAI responses whose structured output was cut off mid-object.
 *
 * Read only. GET /v1/responses/{response_id} for each id you supply, with a
 * project key set to Read Only, and optionally one GET against an Anthropic
 * Message Batches results file, which needs a workspace key.
 *
 * There is no list endpoint for /v1/responses, so the ids come from your own
 * records: one id per line in a file. The finding is a request that succeeded
 * and stopped early, leaving a valid prefix of the answer behind. A prefix of
 * valid JSON is not JSON. The repair is printed, never performed.
 */
import { readFile } from 'node:fs/promises';

const OPENAI_API = 'https://api.openai.com/v1';
const ANTHROPIC_API = 'https://api.anthropic.com/v1';
const ANTHROPIC_VERSION = '2023-06-01';

const FINDINGS = new Set([
  'truncated-by-length', 'ceiling-spent-on-reasoning', 'cut-without-a-reason']);

const REASONING_DOMINANT = 0.6;

/** Concatenate the visible text of a stored response. Pure. Both surfaces. */
export function outputText(response) {
  const parts = [];
  for (const item of response?.output ?? []) {
    for (const content of item?.content ?? []) {
      if (content?.type === 'output_text' || content?.type === 'text') {
        parts.push(String(content.text ?? ''));
      }
    }
  }
  for (const choice of response?.choices ?? []) {
    const text = choice?.message?.content;
    if (typeof text === 'string') parts.push(text);
  }
  return parts.join('');
}

/**
 * Where a JSON document stops. Pure. empty | parses | truncated | malformed.
 * "truncated" means a valid prefix that never closes, which is the difference
 * between an answer that was cut and an answer the model got wrong.
 */
export function jsonState(text) {
  const body = String(text ?? '').trim();
  if (!body) return 'empty';
  try {
    JSON.parse(body);
    return 'parses';
  } catch { /* fall through to the scanner */ }

  let depth = 0;
  let inString = false;
  let escaped = false;
  for (const ch of body) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === '{' || ch === '[') depth += 1;
    else if (ch === '}' || ch === ']') {
      depth -= 1;
      if (depth < 0) return 'malformed';
    }
  }
  return (inString || escaped || depth > 0) ? 'truncated' : 'malformed';
}

/** Why a stored response stopped early, or null. Pure. Both vocabularies. */
export function incompleteReason(response) {
  if (String(response?.status ?? '') === 'incomplete') {
    return String(response?.incomplete_details?.reason ?? 'unknown');
  }
  for (const choice of response?.choices ?? []) {
    const finish = String(choice?.finish_reason ?? '');
    if (finish === 'length') return 'max_output_tokens';
    if (finish === 'content_filter') return 'content_filter';
  }
  return null;
}

/** Does this response carry a refusal rather than an answer? Pure. */
export function hasRefusal(response) {
  for (const item of response?.output ?? []) {
    for (const content of item?.content ?? []) {
      if (content?.type === 'refusal') return true;
    }
  }
  for (const choice of response?.choices ?? []) {
    if (choice?.message?.refusal) return true;
  }
  return false;
}

/** Output tokens as a share of the configured ceiling. Pure. Null, not zero. */
export function ceilingUse(response) {
  const cap = Number(response?.max_output_tokens);
  const used = Number(response?.usage?.output_tokens);
  if (!Number.isFinite(cap) || !Number.isFinite(used) || cap <= 0) return null;
  return Math.min(1, used / cap);
}

/** Share of the output tokens that were never returned to you. Pure. */
export function reasoningShare(response) {
  const total = Number(response?.usage?.output_tokens);
  const reasoning = Number(response?.usage?.output_tokens_details?.reasoning_tokens);
  if (!Number.isFinite(total) || !Number.isFinite(reasoning) || total <= 0) return null;
  return Math.min(1, reasoning / total);
}

/** Classify one stored response. Pure. Four of the states are handoffs. */
export function classify(response) {
  const reason = incompleteReason(response);
  const shape = jsonState(outputText(response));
  const used = ceilingUse(response);
  const atCap = used === null ? ''
    : ` Output sat at ${(used * 100).toFixed(0)}% of the configured ceiling.`;

  if (reason === 'max_output_tokens') {
    const thinking = reasoningShare(response);
    if (thinking !== null && thinking >= REASONING_DOMINANT) {
      return ['ceiling-spent-on-reasoning',
        `Stopped on the output ceiling with ${(thinking * 100).toFixed(0)}% of ` +
        'the output tokens spent on reasoning, so the visible answer barely ' +
        `started.${atCap}`];
    }
    if (shape === 'truncated') {
      return ['truncated-by-length',
        'Stopped on the output ceiling mid-object: the text is a valid prefix ' +
        `that never closes.${atCap}`];
    }
    return ['truncated-by-length',
      `Stopped on the output ceiling. The stored text is ${shape}.${atCap}`];
  }

  if (reason === 'content_filter') {
    return ['stopped-by-filter',
      'Generation was halted by the content filter rather than by the ceiling. ' +
      'That is the refusal note, not this one.'];
  }
  if (reason !== null) {
    return ['incomplete-other',
      `Incomplete for reason '${reason}', which is not an output ceiling.`];
  }

  if (hasRefusal(response)) {
    return ['refused',
      'The response completed and carries a refusal instead of an answer. ' +
      'Nothing was cut. Read the refusal note.'];
  }

  if (shape === 'parses') return ['complete', 'Completed and the stored text parses.'];
  if (shape === 'empty') {
    return ['empty-output',
      'Completed with no text at all, which a ceiling reached during reasoning ' +
      'can also produce without reporting one.'];
  }
  if (shape === 'truncated') {
    return ['cut-without-a-reason',
      'The text stops mid-object and the response reports no reason for it. ' +
      'Read the raw record: a Chat Completions row stored without its ' +
      'finish_reason looks exactly like this.'];
  }
  return ['schema-not-followed',
    'Completed, and the text is broken in a way truncation does not explain. ' +
    'That is an advisory schema, not a ceiling.'];
}

/** The repair for one state, with the numbers from this response. Pure. */
export function repairLines(state, response) {
  const cap = response?.max_output_tokens;
  const used = response?.usage?.output_tokens;

  if (state === 'truncated-by-length') {
    const lines = ['Check that the response completed before parsing anything: ' +
      'branch on status and on incomplete_details.reason, and never hand the ' +
      'text to a JSON parser until it says completed.'];
    if (cap && used) {
      lines.push(`This call was capped at ${cap} output tokens and used ${used} ` +
        'of them. Raise the ceiling above the largest record the schema can ' +
        'emit, with room for reasoning.');
    } else {
      lines.push('Raise the output ceiling above the largest record the schema ' +
        'can emit, with room for reasoning.');
    }
    lines.push('Or reshape the schema so one call emits fewer and shorter ' +
      'fields, and paginate. A long free-text field or an unbounded array ' +
      'inside the schema is the usual cause.');
    return lines;
  }

  if (state === 'ceiling-spent-on-reasoning') {
    return ['The ceiling covers reasoning tokens as well as the answer, and here ' +
      'it was gone before the JSON began. Raise it, or lower the reasoning ' +
      'effort for this call.',
    'A structured-output call that needs no deliberation is the cheapest place ' +
      'to spend less thinking.'];
  }
  if (state === 'cut-without-a-reason') {
    return ['Store the whole response object, not just its text. Without status, ' +
      'incomplete_details and usage there is no way to tell a cut answer from a ' +
      'wrong one after the fact.'];
  }
  if (state === 'stopped-by-filter') {
    return ['Not a ceiling. Handle the filter stop and the refusal channel ' +
      'together, as a first-class branch before parsing.'];
  }
  if (state === 'refused') {
    return ['Not a ceiling. Read the refusal text and surface it; a refusal is ' +
      'an answer, not an error and not a truncation.'];
  }
  if (state === 'schema-not-followed') {
    return ['Not a ceiling. Check whether strict was set on the schema at all, ' +
      'because an advisory schema produces exactly this.'];
  }
  return [];
}

/**
 * Read one line of an Anthropic batch results file. Pure.
 * Results arrive in any order, so custom_id is the only safe key.
 */
export function batchLineVerdict(line) {
  let record;
  try {
    record = JSON.parse(String(line ?? ''));
  } catch {
    return [null, 'unreadable', 'the line is not JSON'];
  }
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    return [null, 'unreadable', 'the line is not an object'];
  }

  const customId = record.custom_id ?? null;
  const result = record.result ?? {};
  if (String(result.type ?? '') !== 'succeeded') {
    return [customId, 'not-succeeded',
      `result type '${String(result.type ?? 'missing')}', which is a different note`];
  }

  const message = result.message ?? {};
  const stop = String(message.stop_reason ?? '');
  const blocks = message.content ?? [];
  const last = blocks.length ? blocks[blocks.length - 1]?.type : null;
  if (stop === 'max_tokens') {
    if (last === 'tool_use') {
      return [customId, 'truncated-tool-use',
        'cut on the ceiling and the final block is an incomplete tool_use, so ' +
        'the arguments cannot be executed at all'];
    }
    const tokens = message.usage?.output_tokens ?? 'an unknown number of';
    return [customId, 'truncated-by-length',
      `cut on the ceiling with ${tokens} output token(s)`];
  }
  return [customId, 'complete', `stop_reason '${stop || 'missing'}'`];
}

async function fetchResponse(key, responseId) {
  const res = await fetch(`${OPENAI_API}/responses/${responseId}`,
    { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 404) return null;
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: this needs a project key that ` +
                    'can read stored responses');
  }
  if (!res.ok) throw new Error(`${res.status} from /responses/${responseId}`);
  return res.json();
}

async function* batchResults(key, batchId) {
  const res = await fetch(`${ANTHROPIC_API}/messages/batches/${batchId}/results`,
    { headers: { 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION } });
  if (!res.ok) throw new Error(`${res.status} from the batch results file`);
  const text = await res.text();
  for (const line of text.split('\n')) if (line.trim()) yield line;
}

async function main() {
  const idsFile = (process.env.RESPONSE_IDS || "dummy-response-ids");
  const batchId = (process.env.BATCH_ID || "dummy-batch-id");
  if (!idsFile && !batchId) {
    console.error('set RESPONSE_IDS (a file of stored response ids) or BATCH_ID ' +
                  '(an Anthropic batch id), or both');
    process.exitCode = 2;
    return;
  }
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';
  let checked = 0;
  let bad = 0;

  if (idsFile) {
    const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
    if (!key) {
      console.error('set OPENAI_API_KEY, a project key set to Read Only');
      process.exitCode = 2;
      return;
    }
    const ids = (await readFile(idsFile, 'utf8')).split('\n')
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
    for (const responseId of ids) {
      const stored = await fetchResponse(key, responseId);
      checked += 1;
      if (stored === null) {
        console.warn(`${'unreadable'.padEnd(26)} ${responseId}  not found. Stored ` +
          'responses expire, and a response created without storage was never readable.');
        continue;
      }
      const [state, detail] = classify(stored);
      const line = `${state.padEnd(26)} ${responseId}  ${detail}`;
      if (FINDINGS.has(state)) {
        bad += 1;
        console.warn(line);
        for (const repair of repairLines(state, stored)) console.warn(`  repair: ${repair}`);
      } else if (state === 'complete') {
        if (showAll) console.log(line);
      } else {
        console.log(line);
        for (const repair of repairLines(state, stored)) console.log(`  note: ${repair}`);
      }
    }
  }

  if (batchId) {
    const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
    if (!key) {
      console.error('set ANTHROPIC_API_KEY, a workspace key, to read batch results');
      process.exitCode = 2;
      return;
    }
    const counts = new Map();
    for await (const line of batchResults(key, batchId)) {
      const [customId, state, detail] = batchLineVerdict(line);
      checked += 1;
      counts.set(state, (counts.get(state) ?? 0) + 1);
      if (state === 'truncated-by-length' || state === 'truncated-tool-use') {
        bad += 1;
        console.warn(`${state.padEnd(26)} ${customId}  ${detail}`);
      }
    }
    for (const state of [...counts.keys()].sort()) {
      console.log(`batch ${batchId}: ${counts.get(state)} line(s) ${state}`);
    }
  }

  console.log(`${checked} response(s) checked, ${bad} cut short`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
