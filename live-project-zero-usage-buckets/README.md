# A live project's usage buckets have been empty for days

A customer asks, politely, when the summaries are coming back. Nobody on the call knows what they mean, because the feature works: the page renders, the job runs, the queue is empty, the error rate is zero and the latency graph is the flattest it has been all year. It is flat because for eleven days that project has sent the API nothing at all. A feature flag went the wrong way in an unrelated release, and every dashboard the team owns measures things that only exist when requests do.

**Full guide with diagrams:** https://www.allanninal.dev/llm/live-project-zero-usage-buckets/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_project_went_quiet.py
node node/openai-project-went-quiet.mjs
```

## Test it

```bash
pytest python/test_openai_project_went_quiet.py
node --test node/openai-project-went-quiet.test.mjs
```
