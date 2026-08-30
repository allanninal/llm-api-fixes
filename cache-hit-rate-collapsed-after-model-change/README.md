# Cache read share stepped down the day the model changed

The migration was a one-line change and it went perfectly: latency improved, quality held, the per-token rate went down. Three weeks later the input bill was up by a third and nobody could see why, because the thing that changed is not on the invoice as a line item. The cache-read share was sixty-eight per cent for a fortnight, dropped to eleven on the day the new model id first appears in the usage report, and has been eleven ever since. The prompt was never edited.

**Full guide with diagrams:** https://www.allanninal.dev/llm/cache-hit-rate-collapsed-after-model-change/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_cache_step_after_model_switch.py
node node/anthropic-cache-step-after-model-switch.mjs
```

## Test it

```bash
pytest python/test_anthropic_cache_step_after_model_switch.py
node --test node/anthropic-cache-step-after-model-switch.test.mjs
```
