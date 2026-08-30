/**
 * Find stored OpenAI responses that carry a refusal nobody read.
 *
 * Read only. GET /v1/responses/{response_id} for each id you supply, with a
 * project key set to Read Only. There is no list endpoint for /v1/responses,
 * so the ids come from your own records: one id per line in a file.
 *
 * Structured Outputs gives a safety refusal its own content type so it does
 * not have to be squeezed into your schema. The response completed, nothing
 * errored, and the field a parser reaches for is simply not where the answer
 * went. One refusal is not a finding; a rate per prompt template is.
 *
 * The repair is printed, never performed.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.openai.com/v1';

const FINDINGS = new Set(['refused', 'refused-after-partial', 'stopped-by-filter']);

const GROUP_FLOOR = 20;

/** Every refusal carried by a stored response. Pure. Both surfaces. */
export function refusals(response) {
  const found = [];
  (response?.output ?? []).forEach((item, index) => {
    for (const content of item?.content ?? []) {
      if (content?.type === 'refusal') {
        found.push({ index, text: String(content.refusal ?? '').trim() });
      }
    }
  });
  (response?.choices ?? []).forEach((choice, index) => {
    const text = choice?.message?.refusal;
    if (text) found.push({ index, text: String(text).trim() });
  });
  return found;
}

/** The text a parser would have reached for. Pure. Not "the answer". */
export function visibleText(response) {
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
  return parts.join('').trim();
}

/** Why the response stopped, in one vocabulary. Pure. Null when it did not. */
export function stopReason(response) {
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

/** What to count refusals against. Pure. Metadata wins over the model id. */
export function groupKey(response) {
  const metadata = response?.metadata ?? {};
  for (const field of ['template', 'prompt_template', 'prompt_id', 'use_case']) {
    if (metadata[field]) return String(metadata[field]);
  }
  if (response?.prompt?.id) return `prompt:${response.prompt.id}`;
  return `model:${String(response?.model ?? 'unknown')}`;
}

/** Classify one stored response. Pure. Refusal against truncation is the split. */
export function classify(response) {
  const declined = refusals(response);
  const text = visibleText(response);
  const reason = stopReason(response);

  if (declined.length) {
    const said = declined[0].text || '(the refusal string was empty)';
    if (text) {
      return ['refused-after-partial',
        `The turn produced ${text.length} character(s) of text and then ` +
        `refused: '${said}'. A reader that concatenates output items ends up ` +
        'storing the preamble as if it were the answer.'];
    }
    return ['refused',
      `Completed with a refusal and no answer: '${said}'. There is nothing to ` +
      'parse and nothing went wrong.'];
  }

  if (reason === 'content_filter') {
    return ['stopped-by-filter',
      'Incomplete because the content filter halted generation. That is the ' +
      'platform stopping the turn, not the model declining it, and the two are ' +
      'worth separating in your metrics.'];
  }
  if (reason === 'max_output_tokens') {
    return ['truncated',
      'Incomplete because the output ceiling was reached. Nothing was refused. ' +
      'Read the truncation note.'];
  }
  if (reason !== null) {
    return ['incomplete-other',
      `Incomplete for reason '${reason}', which is neither a refusal nor a ceiling.`];
  }

  if (!text) {
    return ['empty-answer',
      'Completed, no refusal, and no text either. Check whether the output ' +
      'items are a tool call rather than a message.'];
  }
  return ['answered', `Completed with ${text.length} character(s) of text.`];
}

/**
 * Refusal rate per group. Pure. Rows are [group, state] pairs.
 * Rate stays null below the floor: one refusal in one call is 100%, and
 * printing that teaches people to ignore the report.
 */
export function refusalRate(rows, floor = GROUP_FLOOR) {
  const totals = new Map();
  for (const [group, state] of rows ?? []) {
    const key = String(group);
    if (!totals.has(key)) {
      totals.set(key, { total: 0, refused: 0, filtered: 0, rate: null });
    }
    const row = totals.get(key);
    row.total += 1;
    if (state === 'refused' || state === 'refused-after-partial') row.refused += 1;
    else if (state === 'stopped-by-filter') row.filtered += 1;
  }
  for (const row of totals.values()) {
    if (row.total >= floor) row.rate = (row.refused + row.filtered) / row.total;
  }
  return totals;
}

/** The repair for one state. Pure. */
export function repairLines(state) {
  if (state === 'refused' || state === 'refused-after-partial') {
    return ['Handle refusal as a first-class branch before parsing: if any output ' +
      'content item has type refusal, surface the refusal text to the caller and ' +
      'do not attempt schema parsing at all.',
    'Never treat an empty parsed value as a transport failure. A refusal is a ' +
      'completed answer and retrying it unchanged spends money to be told no again.',
    'Log the refusal rate per prompt template. A spike is almost always a prompt ' +
      'change or a bad input source, not a change in who your users are.'];
  }
  if (state === 'stopped-by-filter') {
    return ['Branch on incomplete_details.reason as well as on the refusal content ' +
      'type. A filter stop is the platform halting the turn and it needs the same ' +
      'caller-facing message as a refusal.',
    'Count filter stops separately from model refusals. They move for different ' +
      'reasons and folding them together hides both.'];
  }
  if (state === 'truncated') {
    return ['Not a refusal. Check the output ceiling before you look at the prompt: ' +
      'the model was interrupted, not unwilling.'];
  }
  if (state === 'empty-answer') {
    return ['Not a refusal either. Inspect the output item types before concluding ' +
      'anything: a function call is not a message.'];
  }
  return [];
}

async function fetchResponse(key, responseId) {
  const res = await fetch(`${API}/responses/${responseId}`,
    { headers: { Authorization: `Bearer ${key}` } });
  if (res.status === 404) return null;
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from OpenAI: this needs a project key that ` +
                    'can read stored responses');
  }
  if (!res.ok) throw new Error(`${res.status} from /responses/${responseId}`);
  return res.json();
}

async function main() {
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  const idsFile = (process.env.RESPONSE_IDS || "dummy-response-ids");
  if (!key || !idsFile) {
    console.error('set OPENAI_API_KEY (a project key set to Read Only) and ' +
                  'RESPONSE_IDS (a file of stored response ids, one per line)');
    process.exitCode = 2;
    return;
  }
  const floor = Number((process.env.FLOOR || "dummy-floor") ?? GROUP_FLOOR);
  const showAll = (process.env.SHOW_ALL || "dummy-show-all") === '1';

  const ids = (await readFile(idsFile, 'utf8')).split('\n')
    .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));

  const rows = [];
  let checked = 0;
  let bad = 0;
  for (const responseId of ids) {
    const stored = await fetchResponse(key, responseId);
    checked += 1;
    if (stored === null) {
      console.warn(`${'unreadable'.padEnd(22)} ${responseId}  not found. Stored ` +
        'responses expire, and a response created without storage was never readable.');
      continue;
    }
    const [state, detail] = classify(stored);
    rows.push([groupKey(stored), state]);
    const line = `${state.padEnd(22)} ${responseId}  ${detail}`;
    if (FINDINGS.has(state)) {
      bad += 1;
      console.warn(line);
      for (const repair of repairLines(state)) console.warn(`  repair: ${repair}`);
    } else if (state === 'answered') {
      if (showAll) console.log(line);
    } else {
      console.log(line);
      for (const repair of repairLines(state)) console.log(`  note: ${repair}`);
    }
  }

  const rates = refusalRate(rows, floor);
  for (const group of [...rates.keys()].sort()) {
    const row = rates.get(group);
    if (row.rate === null) {
      console.log(`${'group'.padEnd(22)} ${group}  ${row.total} response(s), ` +
        `under the floor of ${floor} so no rate is claimed`);
    } else {
      console.warn(`${'group'.padEnd(22)} ${group}  ${(row.rate * 100).toFixed(1)}% ` +
        `of ${row.total} response(s) refused or filtered`);
    }
  }

  console.log(`${checked} response(s) checked, ${bad} refused or filtered`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
