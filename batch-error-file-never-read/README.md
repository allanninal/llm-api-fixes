# the batch left an error_file_id that nothing ever fetched

The Batch API answers in two files. Successes go to output_file_id and failures go to error_file_id, and the ingest code was written against the first one on a day when the test batch had no failures. It has never opened the second. The failures are not lost &mdash; they were written down carefully, one JSON line each, with the custom_id and the reason. They are sitting in a file that has an id, a byte count and an expiry date, and in thirty days they will be gone whether or not anybody looked.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-error-file-never-read/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_batch_error_file_audit.py
node node/openai-batch-error-file-audit.mjs
```

## Test it

```bash
pytest python/test_openai_batch_error_file_audit.py
node --test node/openai-batch-error-file-audit.test.mjs
```
