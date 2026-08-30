import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyJob, errorAdvice, errorEvents, hoursSince, jobRow,
         repairLines } from './openai-fine-tune-failures.mjs';

const NOW = 1_800_000_000;
const HOUR = 3600;

const job = (status, { code = '', param = '', message = '', hoursOld = 1,
                       id = 'ftjob_a1' } = {}) => jobRow({
  id,
  object: 'fine_tuning.job',
  status,
  model: 'gpt-5.6-terra',
  created_at: NOW - Math.trunc(hoursOld * HOUR),
  fine_tuned_model: null,
  trained_tokens: null,
  error: (code || message) ? { code, message, param } : null,
});

test('a failed job names the rejected input and the documented fix', () => {
  const row = job('failed', { code: 'invalid_training_file', param: 'training_file',
                              message: 'The job failed due to an invalid training file.' });
  const [state, detail] = classifyJob(row, NOW, 2);
  assert.equal(state, 'job-failed');
  assert.ok(detail.includes('failed on training_file with invalid_training_file'));
  const lines = repairLines(state, row.code);
  assert.equal(lines[0], errorAdvice('invalid_training_file'));
  assert.ok(lines[0].includes('no trailing blank line'));
  assert.ok(lines.some((l) => l.includes('receipt, not a result')));
});

test('an unknown code is printed and never interpreted', () => {
  const row = job('failed', { code: 'some_new_code_2027', param: 'training_file',
                              message: '...' });
  assert.equal(classifyJob(row, NOW, 2)[0], 'job-failed');
  assert.equal(errorAdvice('some_new_code_2027'), '');
  const lines = repairLines('job-failed', row.code);
  assert.ok(lines[0].includes('some_new_code_2027'));
  assert.ok(lines[0].includes('do not act on a guess'));
  assert.ok(errorAdvice('exceeded_quota').includes('billing problem'));
  assert.ok(errorAdvice('exceeded_quota').includes('Editing the file will not help'));
});

test('hours in validating_files is its own finding', () => {
  const stalled = job('validating_files', { hoursOld: 9.4, id: 'ftjob_b2' });
  const [state, detail] = classifyJob(stalled, NOW, 2);
  assert.equal(state, 'stalled-in-validation');
  assert.ok(detail.includes('9.4 hours in validating_files'));
  assert.ok(repairLines(state).some((l) => l.includes('dead upload')));
  assert.equal(classifyJob(job('validating_files', { hoursOld: 1 }), NOW, 2)[0],
               'validating');
  assert.ok(Math.abs(hoursSince(NOW - 5 * HOUR, NOW) - 5) < 1e-9);
  assert.equal(hoursSince(0, NOW), null);
});

test('a failure with no error object is sent to the events feed', () => {
  const row = job('failed');
  assert.equal(row.code, '');
  assert.equal(row.param, '');
  const [state, detail] = classifyJob(row, NOW, 2);
  assert.equal(state, 'failed-without-error');
  assert.ok(detail.includes('the only account of why'));
  assert.ok(repairLines(state).some((l) => l.includes('/events')));
});

test('a succeeded job is handed to the other note', () => {
  const [state, detail] = classifyJob(job('succeeded', { hoursOld: 200 }), NOW, 2);
  assert.equal(state, 'succeeded');
  assert.ok(detail.includes('a different note'));
  assert.deepEqual(repairLines(state), []);
  assert.equal(classifyJob(job('cancelled'), NOW, 2)[0], 'cancelled');
  assert.equal(classifyJob(job('running'), NOW, 2)[0], 'running');
  assert.equal(classifyJob(job('beaming_up'), NOW, 2)[0], 'unknown-status');
});

test('the events feed is filtered to errors and kept in order', () => {
  const feed = [{ level: 'info', message: 'Created fine-tuning job' },
                { level: 'error', message: 'line 4108 has no assistant message' },
                { level: 'warn', message: '...' },
                { level: 'ERROR', message: 'line 4108 has no assistant message' },
                { level: 'error', message: 'validation failed' },
                'not a dict'];
  assert.deepEqual(errorEvents(feed),
                   ['line 4108 has no assistant message', 'validation failed']);
  assert.deepEqual(errorEvents(null), []);
  assert.equal(jobRow(null).id, '');
  assert.equal(jobRow({ created_at: 'nonsense' }).created_at, 0);
});
