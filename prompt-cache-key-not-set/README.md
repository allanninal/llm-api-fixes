# Cache hits fall away exactly when the fleet scales out

The cached share was fine in staging and fine in the first week of the rollout. Then autoscaling started doing its job, and the discount went the wrong way. At three in the morning the prompt caches at seventy per cent; at two in the afternoon, on the same template, the same model and the same code, it caches at sixteen. Everybody's first instinct is that something about the busy path is different. Nothing about the busy path is different. There are simply more machines in it, and none of them has seen this prefix before.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompt-cache-key-not-set/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_cache_key_routing_scatter.py
node node/openai-cache-key-routing-scatter.mjs
```

## Test it

```bash
pytest python/test_openai_cache_key_routing_scatter.py
node --test node/openai-cache-key-routing-scatter.test.mjs
```
