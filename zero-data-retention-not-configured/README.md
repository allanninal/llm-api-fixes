# Zero data retention is claimed and no project resolves to it

The security questionnaire came back with one line highlighted. Prompts and completions are not retained by the model provider. Somebody wrote that eighteen months ago and it was true of the arrangement negotiated at the time, and nobody has looked since, because there is nothing to look at: no header comes back on a completion saying what the retention mode was, no field in the response, no warning in the logs. Meanwhile four projects have been created since that contract was signed, and each one started life on whatever the organization default happened to be that morning.

**Full guide with diagrams:** https://www.allanninal.dev/llm/zero-data-retention-not-configured/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_data_retention_audit.py
node node/openai-data-retention-audit.mjs
```

## Test it

```bash
pytest python/test_openai_data_retention_audit.py
node --test node/openai-data-retention-audit.test.mjs
```
