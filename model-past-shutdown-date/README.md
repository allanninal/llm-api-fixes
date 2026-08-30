# a model id in use is past its published shutdown date

Nothing was deployed. The key did not rotate. One model id started returning 404 on the same morning for every request, and the message reads exactly like a typo: The model does not exist or you do not have access to it. It existed yesterday. There is no distinct error code for a retired model, no deprecation warning on the successful calls that came before it, and nothing in the response that tells the difference between a model that was shut down and a model name somebody misspelled.

**Full guide with diagrams:** https://www.allanninal.dev/llm/model-past-shutdown-date/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_model_shutdown_audit.py
node node/openai-model-shutdown-audit.mjs
```

## Test it

```bash
pytest python/test_openai_model_shutdown_audit.py
node --test node/openai-model-shutdown-audit.test.mjs
```
