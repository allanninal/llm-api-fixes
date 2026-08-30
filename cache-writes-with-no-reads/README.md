# cache writes are paid for and never read back

Caching was switched on in June and the bill went up. Every call writes a fresh cache entry, is billed 1.25x base input for the privilege, and then nothing ever reads that entry back before it expires. The reason is one line: a request id got templated into the system prompt, ahead of the breakpoint, so no two prefixes have ever been byte-identical. The feature is working exactly as documented and it is costing you money.

**Full guide with diagrams:** https://www.allanninal.dev/llm/cache-writes-with-no-reads/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_cache_write_ratio.py
node node/anthropic-cache-write-ratio.mjs
```

## Test it

```bash
pytest python/test_anthropic_cache_write_ratio.py
node --test node/anthropic-cache-write-ratio.test.mjs
```
