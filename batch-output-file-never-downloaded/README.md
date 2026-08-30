# The batch finished and nobody ever collected the output

The batch ran, the model answered ninety thousand times, the invoice includes every one of those answers, and the file holding them was deleted a month later without ever being opened. There is no incident for this. The batch object still sits in the list saying completed, which is true, and pointing at an id that no longer resolves, which is also true. The only party that ever knew the results were wanted was a process that stopped running in March.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-output-file-never-downloaded/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/batch_output_unclaimed_audit.py
node node/batch-output-unclaimed-audit.mjs
```

## Test it

```bash
pytest python/test_batch_output_unclaimed_audit.py
node --test node/batch-output-unclaimed-audit.test.mjs
```
