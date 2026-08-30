import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ageDays, classState, classifyFile, fileRow, human, referencedIds,
         repairLines, summarise } from './openai-orphaned-assistant-files.mjs';

const NOW = 1_800_000_000;
const DAY = 86400;

const f = (id, size = 1024, purpose = 'assistants', daysOld = 500) => fileRow({
  id, bytes: size, purpose, filename: `${id}.pdf`,
  created_at: NOW - Math.trunc(daysOld * DAY),
});

test('a file no surviving store holds is the finding', () => {
  const row = f('file-3ab', 43200512, 'assistants', 511);
  const [state, detail] = classifyFile(row, new Set(), true, NOW);
  assert.equal(state, 'orphan');
  assert.ok(detail.includes('no surviving vector store holds this id'));
  assert.ok(detail.includes('41.2 MiB') && detail.includes('511 day(s) ago'));
  const lines = repairLines(state, 1, 43200512);
  assert.ok(lines.some((l) => l.includes('DELETE /v1/files/{file_id}')));
  assert.ok(lines.some((l) => l.includes('every vector store holding it')));
});

test('platform generated output is its own state', () => {
  const row = f('file-b19', 120832, 'assistants_output', 502);
  const [state, detail] = classifyFile(row, new Set(), true, NOW);
  assert.equal(state, 'orphan-output');
  assert.ok(detail.includes('code interpreter output'));
  assert.ok(detail.includes('no longer exists'));
  const [held, heldDetail] = classifyFile(f('file-c04'), new Set(['file-c04']), true, NOW);
  assert.equal(held, 'still-referenced');
  assert.ok(heldDetail.includes('still reads it'));
  assert.deepEqual(repairLines(held), []);
});

test('one unreadable store downgrades every verdict in the run', () => {
  const row = f('file-3ab');
  assert.equal(classifyFile(row, new Set(), true, NOW)[0], 'orphan');
  const [state, detail] = classifyFile(row, new Set(), false, NOW);
  assert.equal(state, 'subtraction-incomplete');
  assert.ok(detail.includes('could not be listed'));
  assert.ok(detail.includes('cannot be called an orphan'));
  assert.equal(classifyFile(f('file-c04'), new Set(['file-c04']), false, NOW)[0],
               'subtraction-incomplete');
  assert.equal(classState([row], false)[0], 'subtraction-unsafe');
  const lines = repairLines('subtraction-incomplete', 0, 0, ['vs_b2', 'vs_a1']);
  assert.ok(lines[0].includes('vs_a1, vs_b2'));
  assert.ok(lines[0].includes('perfectly well referenced'));
});

test('referencedIds reads the store files own id', () => {
  const ids = referencedIds([{ id: 'file-c04', object: 'vector_store.file',
                               vector_store_id: 'vs_a1', status: 'completed' },
                             { id: 'file-d15', status: 'failed' },
                             { id: '' }, null, 'not-an-object', {}]);
  assert.deepEqual([...ids].sort(), ['file-c04', 'file-d15']);
  assert.equal(referencedIds(null).size, 0);
  assert.equal(classifyFile(f('file-d15'), ids, true, NOW)[0], 'still-referenced');
});

test('an empty purpose class is an answer and not a blank', () => {
  const [state, detail] = classState([], true);
  assert.equal(state, 'class-empty');
  assert.ok(detail.includes('nothing was left behind'));
  assert.deepEqual(repairLines(state), []);
  const [full, fullDetail] = classState([f('file-1'), f('file-2')], true);
  assert.equal(full, 'class-populated');
  assert.ok(fullDetail.includes('2 file(s)'));
  assert.ok(fullDetail.includes('no longer exists'));
});

test('the folds and the formatting survive junk', () => {
  const graded = [['orphan', f('file-1', 1024)],
                  ['orphan', f('file-2', 2048)],
                  ['still-referenced', f('file-3', 4096)]];
  assert.deepEqual(summarise(graded).orphan, { count: 2, bytes: 3072 });
  assert.deepEqual(summarise([]), {});
  assert.equal(fileRow(null).id, '');
  assert.equal(fileRow({ bytes: 'nope', created_at: 'nope' }).size, 0);
  assert.equal(ageDays(0, NOW), null);
  assert.equal(ageDays('x', NOW), null);
  assert.equal(human(1024), '1.0 KiB');
  assert.equal(human(null), '0 B');
});
