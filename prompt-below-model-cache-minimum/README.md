# Prompt sits under the model's cache minimum, so nothing caches

The cache_control breakpoint has been in that request builder since March, and the Haiku route has never reported a cache read. Not a small number: zero, in every daily bucket, on a key whose Opus traffic caches beautifully off the same code path. Nothing errored, because nothing was wrong. The prefix is about fifteen hundred tokens. Opus 5 starts caching at five hundred and twelve. Haiku 4.5 does not start until four thousand and ninety six. The parameter was accepted and thrown away.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompt-below-model-cache-minimum/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_cache_floor_bracket.py
node node/anthropic-cache-floor-bracket.mjs
```

## Test it

```bash
pytest python/test_anthropic_cache_floor_bracket.py
node --test node/anthropic-cache-floor-bracket.test.mjs
```
