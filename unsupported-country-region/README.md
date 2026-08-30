# The same key works on your laptop and 403s in production

The feature works. It works on two laptops, it works in CI, it worked in the preview deployment, and it has never once failed in review. It is promoted to production on a Thursday and every request returns 403. Not a rate limit, not an expired key, not a model id: a flat refusal with a message about countries. Nobody changed the code. Nobody rotated the key. What changed is that the edge platform picked a point of presence closer to the user, and the request now leaves the internet somewhere the provider does not serve.

**Full guide with diagrams:** https://www.allanninal.dev/llm/unsupported-country-region/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/llm_egress_region_probe.py
node node/llm-egress-region-probe.mjs
```

## Test it

```bash
pytest python/test_llm_egress_region_probe.py
node --test node/llm-egress-region-probe.test.mjs
```
