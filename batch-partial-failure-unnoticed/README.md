# a batch reads completed while some of its rows failed

The nightly job submits fifty thousand rows, sleeps, polls until the status turns to completed, downloads the output file and loads it. It has done that every night for eight months. The table it fills is short &mdash; not empty, not obviously wrong, just a few hundred rows smaller than the input file, by a number that changes every night. No exception was ever raised. The batch object says completed in plain text, and three fields further down it says "failed": 869, which nothing has ever read.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-partial-failure-unnoticed/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_batch_partial_failure_audit.py
node node/openai-batch-partial-failure-audit.mjs
```

## Test it

```bash
pytest python/test_openai_batch_partial_failure_audit.py
node --test node/openai-batch-partial-failure-audit.test.mjs
```
