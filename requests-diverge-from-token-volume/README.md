# Request count tripled while token volume stayed flat

Latency got worse over about a fortnight, in the way that nobody can date precisely. Then 429s started arriving at a volume that used to be comfortable, and the obvious explanation was growth, except that the invoice barely moved. Two numbers sit in the same usage report and they have come apart: requests are up three times and tokens are up not at all. Two thirds of the extra calls landed in seventeen hours out of a hundred and sixty-eight, which is not what a new customer looks like.

**Full guide with diagrams:** https://www.allanninal.dev/llm/requests-diverge-from-token-volume/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_retry_storm_shape.py
node node/openai-retry-storm-shape.mjs
```

## Test it

```bash
pytest python/test_openai_retry_storm_shape.py
node --test node/openai-retry-storm-shape.test.mjs
```
