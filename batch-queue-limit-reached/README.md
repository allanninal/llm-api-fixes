# The batch queue is full, so the next submission is refused

Nothing has failed. Every batch in the account is healthy, every one of them is inside its window, and the Messages API limits are not being touched. The only symptom is that the submitter has started getting 429s, and it is getting them for a reason that has nothing to do with the requests it is sending: an unrelated job, in an unrelated workspace, has parked four hundred thousand requests in a queue that the whole organization shares.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-queue-limit-reached/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_batch_queue_depth.py
node node/anthropic-batch-queue-depth.mjs
```

## Test it

```bash
pytest python/test_anthropic_batch_queue_depth.py
node --test node/anthropic-batch-queue-depth.test.mjs
```
