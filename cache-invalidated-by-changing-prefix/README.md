# Cache written on every call by a prefix that keeps moving

Caching went in six weeks ago and the cached share never moved off zero. The obvious explanations were checked and cleared: the breakpoint is there, the prefix is long, the traffic is constant. What nobody looked at until somebody pulled the minute buckets is that the writes are not occasional. There is a write in every single minute of the window, one after another for four hours, and not one read anywhere in them. A five minute entry written at 14:03 was still alive at 14:07, and the call at 14:07 wrote a new one.

**Full guide with diagrams:** https://www.allanninal.dev/llm/cache-invalidated-by-changing-prefix/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_cache_prefix_churn.py
node node/anthropic-cache-prefix-churn.mjs
```

## Test it

```bash
pytest python/test_anthropic_cache_prefix_churn.py
node --test node/anthropic-cache-prefix-churn.test.mjs
```
