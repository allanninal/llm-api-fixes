/**
 * Find the day system_fingerprint moved, using completions you already stored.
 *
 * Read only, and it sends nothing at all. One paged GET of
 * /v1/chat/completions, which lists chat completions created with store set to
 * true. No canary is posted: that would generate and bill, and it would only
 * describe the backend serving one request at the moment the script ran.
 *
 * The field is optional. Where it is empty on every stored completion for a
 * model, that is a finding, because a signal you cannot read is not a signal.
 */
const LIST_URL = 'https://api.openai.com/v1/chat/completions';

export const MEASURED =
  'measured: distinct system_fingerprint values on completions you already made';

const FINDINGS = new Set(['fingerprint-moved', 'fingerprint-absent', 'nothing-stored']);

/** A UTC timestamp string. Pure. Empty for anything unusable. */
export function iso(ts) {
  const seconds = Number(ts);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  return `${new Date(seconds * 1000).toISOString().slice(0, 19)}Z`;
}

/** Rows from listing pages. Pure. A missing fingerprint becomes ''. */
export function flatten(pages) {
  const rows = [];
  for (const page of pages ?? []) {
    for (const item of page?.data ?? []) {
      if (!item || typeof item !== 'object') continue;
      const created = Number(item.created ?? 0);
      rows.push({
        id: String(item.id ?? ''),
        created: Number.isFinite(created) ? Math.trunc(created) : 0,
        model: String(item.model ?? '(unknown)'),
        fingerprint: String(item.system_fingerprint ?? ''),
      });
    }
  }
  return rows;
}

/** Rows created at or after cutoff. Pure. The clock is passed in. */
export function within(rows, cutoff) {
  if (!cutoff) return [...(rows ?? [])];
  return (rows ?? []).filter((r) => Number(r?.created ?? 0) >= Number(cutoff));
}

/** {model: [row]} sorted by created. Pure. Order is the whole finding. */
export function byModel(rows) {
  const grouped = {};
  for (const row of rows ?? []) {
    const model = row?.model || '(unknown)';
    (grouped[model] ??= []).push(row);
  }
  for (const model of Object.keys(grouped)) {
    grouped[model].sort((a, b) => (a.created - b.created)
      || String(a.id).localeCompare(String(b.id)));
  }
  return grouped;
}

/** [[created, old, new]] where consecutive fingerprints differ. Pure. */
export function transitions(rows) {
  const out = [];
  let previous = '';
  for (const row of rows ?? []) {
    const current = String(row?.fingerprint ?? '');
    if (!current) continue;
    if (previous && current !== previous) {
      out.push([Math.trunc(Number(row.created ?? 0)), previous, current]);
    }
    previous = current;
  }
  return out;
}

/** True when a fingerprint reappears after another one. Pure. */
export function interleaved(rows) {
  const runs = [];
  for (const row of rows ?? []) {
    const current = String(row?.fingerprint ?? '');
    if (!current) continue;
    if (!runs.length || runs[runs.length - 1] !== current) runs.push(current);
  }
  return runs.length > new Set(runs).size;
}

/** Grade one model. Pure. Returns [state, detail]. */
export function verdict(model, rows) {
  const list = [...(rows ?? [])];
  const withFp = list.filter((r) => r?.fingerprint);
  const distinct = [...new Set(withFp.map((r) => r.fingerprint))].sort();
  if (!list.length) {
    return ['nothing-stored', `no stored completions for ${model} in this window`];
  }
  if (!withFp.length) {
    return ['fingerprint-absent',
      `no stored completion on ${model} carries a system_fingerprint, so a `
      + 'backend change cannot be detected here even in principle'];
  }
  if (distinct.length === 1 && withFp.length === 1) {
    return ['single-observation',
      `one stored completion on ${model} carries a fingerprint, which is a `
      + 'reading and not a comparison'];
  }
  if (distinct.length === 1) {
    return ['fingerprint-stable',
      `${model} ran under one backend configuration across ${withFp.length} `
      + 'stored completions. seed is documented as best effort, so this is the '
      + 'parameter behaving rather than a guarantee'];
  }
  const shape = interleaved(list)
    ? 'interleaving, so more than one configuration is being served at once'
    : 'switching once';
  return ['fingerprint-moved',
    `${model} ran under ${distinct.length} backend configurations in this `
    + `window, ${shape}`];
}

/** The repair for one verdict. Pure. Printed, never performed. */
export function repairLines(state, mixed = false) {
  if (state === 'fingerprint-moved') {
    const lines = ['stop using seed as a cache key or a test oracle. Assert on '
      + 'structure and semantics, and record system_fingerprint beside every '
      + 'baseline so a change explains a diff instead of failing a build.',
      'pin the model snapshot rather than a floating alias, so at least the '
      + 'weights are not a second moving part.'];
    if (mixed) {
      lines.push('the values interleave rather than switching once, so two calls '
        + 'made minutes apart can land on different configurations. Re-recording '
        + 'baselines will not fix that; only caching your own responses will.');
    }
    return lines;
  }
  if (state === 'fingerprint-absent') {
    return ['do not build reproducibility on seed for this model. There is no '
      + 'signal to alarm on, so cache your own responses instead.',
      'if a test needs stability, freeze the response in the fixture rather than '
      + 'asking the platform to reproduce it.'];
  }
  if (state === 'nothing-stored') {
    return ['nothing was stored, so this question cannot be answered from the '
      + 'API. Set store: true on a sample of traffic, or accept that '
      + 'reproducibility has no evidence behind it.',
      'note that the Responses API object carries neither seed nor '
      + 'system_fingerprint, and /v1/responses cannot be listed, so a migration '
      + 'onto it removes this reading entirely.'];
  }
  if (state === 'fingerprint-stable') {
    return ['nothing to do today. Keep this run on a schedule: the value held '
      + 'across the window, which is best effort holding, not a promise that it '
      + 'will.'];
  }
  if (state === 'single-observation') {
    return ['store more traffic or widen the window. One fingerprint is a '
      + 'reading, and this note needs two to say anything.'];
  }
  return [];
}

async function fetchPages(key, model, metadata) {
  const pages = [];
  const params = new URLSearchParams({ limit: '100', order: 'asc' });
  if (model) params.set('model', model);
  for (const pair of metadata ?? []) {
    const at = String(pair).indexOf('=');
    if (at > 0) {
      params.set(`metadata[${pair.slice(0, at).trim()}]`, pair.slice(at + 1).trim());
    }
  }
  for (let page = 0; page < 200; page += 1) {
    const url = `${LIST_URL}?${params.toString()}`;
    let res;
    try {
      res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
    } catch (err) {
      return [pages, `request failed: ${err.message}`];
    }
    if (res.status !== 200) {
      return [pages, `HTTP ${res.status} ${(await res.text()).slice(0, 160)}`];
    }
    const body = await res.json();
    pages.push(body);
    if (!body.has_more || !body.last_id) break;
    params.set('after', body.last_id);
  }
  return [pages, null];
}

function args(argv) {
  const out = { days: 30, metadata: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--days') out.days = Number.parseInt(argv[i += 1], 10);
    else if (argv[i] === '--model') out.model = argv[i += 1];
    else if (argv[i] === '--metadata') out.metadata.push(argv[i += 1]);
  }
  return out;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const key = (process.env.OPENAI_API_KEY || "dummy-openai-api-key");
  if (!key) {
    console.error('set OPENAI_API_KEY to a project key set to Read Only. It is '
      + 'used for one paged GET of /v1/chat/completions');
    process.exitCode = 2;
    return;
  }
  const [pages, err] = await fetchPages(key, opts.model, opts.metadata);
  if (err) {
    console.error(err);
    process.exitCode = 2;
    return;
  }
  const cutoff = Math.trunc(Date.now() / 1000) - (opts.days || 30) * 86400;
  const rows = within(flatten(pages), cutoff);
  const grouped = byModel(rows);
  let findings = 0;

  if (!rows.length) {
    const [state, detail] = verdict('(any model)', []);
    console.log(`${state.padEnd(20)} ${detail}`);
    for (const line of repairLines(state)) console.log(`  repair: ${line}`);
    console.log('1 finding(s)');
    process.exitCode = 1;
    return;
  }

  for (const model of Object.keys(grouped).sort()) {
    const entries = grouped[model];
    const withFp = entries.filter((r) => r.fingerprint);
    const distinct = new Set(withFp.map((r) => r.fingerprint));
    console.log(`${model.padEnd(20)} ${entries.length} stored, ${withFp.length} `
      + `with a fingerprint, ${distinct.size} distinct`);
    for (const [created, old, next] of transitions(entries)) {
      console.log(`  ${old} -> ${next}  at ${iso(created)}`);
    }
    const [state, detail] = verdict(model, entries);
    console.log(`${state.padEnd(20)} ${detail}`);
    if (state === 'fingerprint-moved') {
      console.log(`  ${MEASURED}`);
      console.log('  inferred: that output recorded before the switch is not '
        + 'reproducible after it');
    }
    for (const line of repairLines(state, interleaved(entries))) {
      console.log(`  repair: ${line}`);
    }
    if (FINDINGS.has(state)) findings += 1;
  }

  console.log(`${findings} finding(s)`);
  process.exitCode = findings ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
