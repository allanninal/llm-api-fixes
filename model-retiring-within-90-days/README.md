# a model you still call retires in under 90 days

Every call is returning 200. Latency is normal, cost is normal, the evals are green. The only thing wrong is a field nobody is reading: shutdown_date on the model you route most of your traffic to is a real date, and it is close. Nothing will change until that morning, and then everything will, all at once, in every code path that names the id.

**Full guide with diagrams:** https://www.allanninal.dev/llm/model-retiring-within-90-days/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_model_retirement_window.py
node node/openai-model-retirement-window.mjs
```

## Test it

```bash
pytest python/test_openai_model_retirement_window.py
node --test node/openai-model-retirement-window.test.mjs
```
