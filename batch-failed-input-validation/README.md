# The batch failed validation and named the broken line

The submitter returned 200 and the run log says the nightly enrichment fired. Sixteen hours later the enrichment table is exactly as long as it was yesterday. There was no exception, no alert and no retry, because from the client's point of view nothing went wrong: the batch was accepted. It failed forty seconds later, in a state your code never looks at, and it has been holding a list of line numbers ever since.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-failed-input-validation/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_batch_validation_audit.py
node node/openai-batch-validation-audit.mjs
```

## Test it

```bash
pytest python/test_openai_batch_validation_audit.py
node --test node/openai-batch-validation-audit.test.mjs
```
