# Every scheduled run starts on a cache that was evicted

The nightly enrichment job has run at 02:00 for a year and its prompt has not changed since June. Its cached share is zero. Not low, and not erratic &mdash; zero, on the first hour, every single night, while the two hours that follow it cache at seventy-five per cent off the same prefix. Nothing is misconfigured. The entry written at 02:00 last night was evicted somewhere around 02:40, and by the time the job came back twenty-three hours later there was nothing left to match.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompt-cache-retention-left-at-default/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_cache_cold_after_idle.py
node node/openai-cache-cold-after-idle.mjs
```

## Test it

```bash
pytest python/test_openai_cache_cold_after_idle.py
node --test node/openai-cache-cold-after-idle.test.mjs
```
