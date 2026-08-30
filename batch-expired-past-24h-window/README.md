# a batch expired when the 24 hour completion window closed

The submission returned 200 yesterday afternoon. The poller has been asking for the batch ever since, testing the status against completed, and it has never matched, so as far as the job is concerned the work is still running. It is not. Twenty-four hours after the batch started processing, everything OpenAI had not got to was abandoned, the status went to expired, and thirty thousand rows landed in the error file with the code batch_expired. Nothing raised, nothing retried, and the poller is still waiting.

**Full guide with diagrams:** https://www.allanninal.dev/llm/batch-expired-past-24h-window/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_batch_expiry_audit.py
node node/openai-batch-expiry-audit.mjs
```

## Test it

```bash
pytest python/test_openai_batch_expiry_audit.py
node --test node/openai-batch-expiry-audit.test.mjs
```
