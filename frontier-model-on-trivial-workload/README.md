# a frontier model is answering twenty-token questions

Somebody built the intent router in an afternoon eighteen months ago. They pasted the model name out of the quickstart, because on that afternoon the question was whether the thing worked at all and the answer was worth whatever it cost. It works. It has worked every day since, four hundred thousand times a month, and every one of those calls returns a single word from a list of nine. The model that returns it is the most expensive one the organization can buy.

**Full guide with diagrams:** https://www.allanninal.dev/llm/frontier-model-on-trivial-workload/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_model_rightsizing_audit.py
node node/openai-model-rightsizing-audit.mjs
```

## Test it

```bash
pytest python/test_openai_model_rightsizing_audit.py
node --test node/openai-model-rightsizing-audit.test.mjs
```
