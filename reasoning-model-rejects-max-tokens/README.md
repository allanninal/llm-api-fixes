# Requests billed, zero output tokens: max_tokens refused

The model constant changed in a one-line pull request, because the old id has a shutdown date and somebody diaried it properly. The deploy went out on Thursday. Nothing paged: the endpoint returns a 500 to the user and the retry wrapper swallows it, and the error-rate dashboard is scoped to the gateway rather than to this worker. What the organization usage report shows for Friday is eleven thousand requests against the new model, no input tokens, and no output tokens at all.

**Full guide with diagrams:** https://www.allanninal.dev/llm/reasoning-model-rejects-max-tokens/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_zero_output_buckets.py
node node/openai-zero-output-buckets.mjs
```

## Test it

```bash
pytest python/test_openai_zero_output_buckets.py
node --test node/openai-zero-output-buckets.test.mjs
```
