# The flex tier fails by not being served, and bills nothing

The nightly enrichment job was moved to flex processing because it is not urgent and flex is priced like batch. It has been fine for six weeks. It is still fine, in the sense that nothing has ever paged: the job logs a count at the end, the count is lower some nights, and the difference is a few thousand records that quietly did not get enriched. The invoice is lower too, which is exactly what everybody expected to see, and which is why nobody looked.

**Full guide with diagrams:** https://www.allanninal.dev/llm/flex-resource-unavailable-timeouts/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_flex_tier_served.py
node node/openai-flex-tier-served.mjs
```

## Test it

```bash
pytest python/test_openai_flex_tier_served.py
node --test node/openai-flex-tier-served.test.mjs
```
