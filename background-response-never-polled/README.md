# The background response is queued and nothing is polling

The queue worker takes a job, starts a background response, writes the id somewhere, and returns. It is a good design: the request is accepted in a few hundred milliseconds and the model can take twenty minutes without holding a socket open. Then the worker is redeployed mid-shift, or the process that was going to poll gets an unhandled exception on a different code path, and the ids stop being read. The jobs keep running. They keep billing. And the only place their results exist is on an object nobody is asking about.

**Full guide with diagrams:** https://www.allanninal.dev/llm/background-response-never-polled/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_background_response_audit.py
node node/openai-background-response-audit.mjs
```

## Test it

```bash
pytest python/test_openai_background_response_audit.py
node --test node/openai-background-response-audit.test.mjs
```
