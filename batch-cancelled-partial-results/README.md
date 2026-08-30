# Cancelling a batch does not unbill the rows it already ran

Somebody hit cancel during the deploy, which was the right call, and then the runbook said re-run it in the morning. So it was re-run in the morning. What nobody checked is that the batch had already processed sixty-one thousand of its ninety thousand rows before the cancel landed, that those rows are sitting in the output file, and that the morning re-run paid for all sixty-one thousand of them a second time. Cancel is not a rollback. It is a stop.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-cancelled-partial-results/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/batch_cancellation_audit.py
node node/batch-cancellation-audit.mjs
```

## Test it

```bash
pytest python/test_batch_cancellation_audit.py
node --test node/batch-cancellation-audit.test.mjs
```
