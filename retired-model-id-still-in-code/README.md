# a retired model id still sitting in the code

A batch job that runs on the first of the month failed on every request with 404 and "type": "not_found_error", message The requested resource could not be found. The endpoint is right, the key works, the same key runs the rest of the application all day. The model id in that job's params block was retired months ago, and nothing else in the codebase names it, so nothing else broke and nobody knew.

**Full guide with diagrams:** https://www.allanninal.dev/llm/retired-model-id-still-in-code/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_model_ids_audit.py
node node/anthropic-model-ids-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_model_ids_audit.py
node --test node/anthropic-model-ids-audit.test.mjs
```
