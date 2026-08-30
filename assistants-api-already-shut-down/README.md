# The Assistants API is shut down. Is yours still answering?

The pipeline has not run since Wednesday and the error is a 404 on a path that has been in the codebase for two years. Somebody checks the model id first, because that is what a 404 usually means, and the model id is fine. It is fine because the model was never the problem: the whole /v1/assistants family reached its published shutdown date on 26 August 2026 and the endpoint is simply not there any more. The uncomfortable part comes an hour later, when the same probe run against the staging organization returns 200, and now you have two organizations, one dead API, and a question nobody wants to answer about how long the other one has.

**Full guide with diagrams:** https://www.allanninal.dev/llm/assistants-api-already-shut-down/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/assistants_shutdown_probe.py
node node/assistants-shutdown-probe.mjs
```

## Test it

```bash
pytest python/test_assistants_shutdown_probe.py
node --test node/assistants-shutdown-probe.test.mjs
```
