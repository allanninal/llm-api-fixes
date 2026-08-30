/**
 * Compare the context window a Claude model reports with the one your code enforces.
 *
 * Read only. One GET per configured model id against the Models API with a
 * workspace key. No message is ever sent.
 *
 * There is deliberately no beta-header probe. GET /v1/models with
 * anthropic-beta: context-1m-2025-08-07 returns 200 whether or not the beta
 * does anything, because the name is still recognised. Acceptance is not
 * effect.
 */
import { readFile } from 'node:fs/promises';

const API = 'https://api.anthropic.com/v1';
const VERSION = '2023-06-01';

const BETA_1M = 'context-1m-2025-08-07';
const BETA_RETIRED_ON = '2026-04-30';
const STANDARD_WINDOW = 200_000;
const LONG_WINDOW = 1_000_000;

const FINDINGS = new Set(['capped-in-code', 'ceiling-below-model',
                          'cap-above-model', 'inert-beta-header',
                          'retired-beta', 'phantom-premium']);

/** Read a positive integer, or null. Pure. Absent is never zero. */
export function readInt(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const whole = Math.trunc(n);
  return whole > 0 ? whole : null;
}

/**
 * Is this a plausible model id? Pure.
 * The guard that stops a config value becoming a URL path segment.
 */
export function validModelId(modelId) {
  const text = String(modelId ?? '').trim();
  if (!text || text.length > 128) return false;
  return /^[A-Za-z][A-Za-z0-9._-]*$/.test(text);
}

/** Read the declared per-model rules. Pure. Invalid ids are dropped. */
export function parseRules(config) {
  const rules = {};
  for (const [modelId, raw] of Object.entries(config ?? {})) {
    if (!validModelId(modelId)) continue;
    const row = raw && typeof raw === 'object' ? raw : {};
    let betas = row.beta_headers;
    if (typeof betas === 'string') betas = [betas];
    rules[String(modelId).trim()] = {
      cap: readInt(row.max_input_tokens),
      betas: (betas ?? []).map((b) => String(b ?? '').trim().toLowerCase())
        .filter((b) => b.length > 0),
      premium: Boolean(row.long_context_premium),
    };
  }
  return rules;
}

/** max_input_tokens off the model object. Pure. Null when absent. */
export function reportedWindow(model) {
  return readInt(model?.max_input_tokens);
}

/** max_output_tokens off the model object. Pure. Context, never graded here. */
export function reportedOutput(model) {
  return readInt(model?.max_output_tokens);
}

/** Tokens of window that exist and cannot be reached. Pure. Null when unknown. */
export function shortfall(reported, enforced) {
  if (reported === null || enforced === null) return null;
  return Math.max(0, reported - enforced);
}

/** The enforced ceiling against the reported window. Pure. [state, detail] or null. */
export function gradeCeiling(reported, enforced) {
  if (reported === null) {
    return ['window-not-reported',
      'the model object carried no max_input_tokens, so no claim is made ' +
      'about the enforced ceiling'];
  }
  if (enforced === null) return null;
  if (enforced > reported) {
    return ['cap-above-model',
      `model reports ${reported}, code enforces ${enforced}: the first ` +
      'request over the reported window returns 400 prompt is too long'];
  }
  const gap = shortfall(reported, enforced);
  if (gap === 0) {
    return ['aligned', `model reports ${reported}, code enforces ${enforced}`];
  }
  if (reported >= LONG_WINDOW && enforced <= STANDARD_WINDOW) {
    return ['capped-in-code',
      `model reports ${reported}, code enforces ${enforced}: ${gap} token(s) ` +
      'of window bought and unreachable'];
  }
  return ['ceiling-below-model',
    `model reports ${reported}, code enforces ${enforced}: ${gap} token(s) ` +
    'of window left unused'];
}

/** Every declared beta header against the window the model reports. Pure. */
export function gradeBetas(reported, betas) {
  const out = [];
  for (const beta of betas ?? []) {
    if (beta !== BETA_1M || reported === null) continue;
    if (reported >= LONG_WINDOW) {
      out.push(['inert-beta-header',
        `${BETA_1M} is sent here and does nothing: the 1M window is the ` +
        'default on this model and needs no header']);
    } else {
      out.push(['retired-beta',
        `model reports ${reported} and ${BETA_1M} was retired for the Sonnet ` +
        `4.5 and Sonnet 4 family on ${BETA_RETIRED_ON}: over the standard ` +
        'window this id now returns 400, header or not']);
    }
  }
  return out;
}

/** A surviving long-context price or throttle branch. Pure. Null when absent. */
export function gradePremium(reported, premium) {
  if (!premium) return null;
  if (reported === null || reported < LONG_WINDOW) return null;
  return ['phantom-premium',
    'a long-context price or throttle branch is declared for this model, and ' +
    'there is no long-context premium: a 900k-token request bills at the same ' +
    'per-token rate as a 9k one, and the dedicated 1M rate limits were removed'];
}

/**
 * Every finding for one model. Pure. Returns a list of [state, detail].
 * A list rather than one state: a stale id routinely carries a frozen ceiling,
 * an inert header and a phantom premium at the same time.
 */
export function audit(model, rule) {
  const row = rule ?? {};
  const reported = reportedWindow(model);
  const out = [];
  const ceiling = gradeCeiling(reported, row.cap ?? null);
  if (ceiling !== null) out.push(ceiling);
  out.push(...gradeBetas(reported, row.betas));
  const premium = gradePremium(reported, row.premium);
  if (premium !== null) out.push(premium);
  return out;
}

/** The repair for one finding. Pure. Printed, never performed. */
export function repairLines(state, modelId) {
  if (state === 'capped-in-code') {
    return [
      `raise the enforced ceiling for ${modelId} to the window the model ` +
      'reports, then delete the truncation path that exists to serve the old one.',
      'read the ceiling from the model object at start-up instead of ' +
      'hardcoding it, and this cannot drift again when the id rotates.',
    ];
  }
  if (state === 'ceiling-below-model') {
    return [`the enforced ceiling for ${modelId} is below the reported window. ` +
            'Confirm that is deliberate rather than inherited.'];
  }
  if (state === 'cap-above-model') {
    return ['this direction fails loudly rather than quietly: count a real ' +
            'payload against the reported window before you send it.'];
  }
  if (state === 'inert-beta-header') {
    return [`delete ${BETA_1M} from the request path for ${modelId}. It is not ` +
            'harmful and it is not doing anything, and leaving it in is what ' +
            'keeps the rest of the obsolete branch alive.'];
  }
  if (state === 'retired-beta') {
    return ['over the standard window this id now returns 400 whatever the ' +
            'header says. The path forward is a 4.6 or later id, where 1M is ' +
            'the default and no header is involved.'];
  }
  if (state === 'phantom-premium') {
    return ['delete the premium branch and the separate long-context throttle. ' +
            'Standard account rate limits apply at every context length now.'];
  }
  return [];
}

async function get(key, path) {
  const res = await fetch(API + path, {
    headers: { 'x-api-key': key, 'anthropic-version': VERSION },
  });
  if (res.status === 401 || res.status === 403) {
    throw new Error(`${res.status} from Anthropic: ANTHROPIC_API_KEY needs to ` +
                    'be a workspace key that can read the Models API');
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

async function main() {
  const key = (process.env.ANTHROPIC_API_KEY || "dummy-anthropic-api-key");
  if (!key) {
    console.error('set ANTHROPIC_API_KEY to a workspace key that can read the ' +
                  'Models API');
    process.exitCode = 2;
    return;
  }
  const file = process.argv.slice(2).find((a) => !a.startsWith('--'));
  if (!file) {
    console.error('pass a JSON file of per-model rules: enforced ceiling, beta ' +
                  'headers, and whether a long-context price branch exists');
    process.exitCode = 2;
    return;
  }

  let rules;
  try {
    rules = parseRules(JSON.parse(await readFile(file, 'utf8')));
  } catch (err) {
    console.error(`could not read ${file}: ${err.message}`);
    process.exitCode = 2;
    return;
  }
  const ids = Object.keys(rules).sort();
  if (ids.length === 0) {
    console.error(`no valid model ids in ${file}`);
    process.exitCode = 2;
    return;
  }

  console.log(`no beta-header probe is made: ${BETA_1M} is still a recognised ` +
              'name, so a 200 would prove the name is valid and nothing about ' +
              'its effect');

  let bad = 0;
  for (const modelId of ids) {
    const model = await get(key, `/models/${modelId}`);
    if (model === null) {
      console.warn(`${'unknown-model-id'.padEnd(20)} ${modelId.padEnd(26)} the ` +
                   'id no longer resolves on the Models API, which is a ' +
                   'retirement rather than a ceiling problem');
      bad += 1;
      continue;
    }

    for (const [state, detail] of audit(model, rules[modelId])) {
      const line = `${state.padEnd(20)} ${modelId.padEnd(26)} ${detail}`;
      if (FINDINGS.has(state)) {
        bad += 1;
        console.warn(line);
        for (const repair of repairLines(state, modelId)) {
          console.warn(`  repair: ${repair}`);
        }
      } else {
        console.log(line);
      }
    }

    const out = reportedOutput(model);
    if (out !== null) {
      console.log(`${'output-ceiling'.padEnd(20)} ${modelId.padEnd(26)} reports ` +
                  `max_output_tokens ${out}, which is a separate ceiling and ` +
                  'is not graded here');
    }
  }

  console.log(`${ids.length} model(s) checked, ${bad} finding(s)`);
  process.exitCode = bad ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => { console.error(err.message); process.exitCode = 2; });
}
