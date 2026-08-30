# scheduled jobs pay full price for work the Batch API halves

Nothing here is broken. No request failed, no row is missing, and no alert should have fired. The nightly enrichment job fires forty thousand completions between 02:00 and 02:20, finishes cleanly, and does it again the next night. It has no user waiting on it and no latency requirement of any kind, and it is being billed at the interactive rate because the synchronous endpoint is what the SDK example used. This is not a bug report. It is an invoice roughly twice the size it needs to be.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-discount-left-unused/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_batch_discount_audit.py
node node/openai-batch-discount-audit.mjs
```

## Test it

```bash
pytest python/test_openai_batch_discount_audit.py
node --test node/openai-batch-discount-audit.test.mjs
```
