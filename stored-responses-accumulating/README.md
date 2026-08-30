# Every response is stored and no endpoint will list them

The question arrives from legal, not from engineering, and it is one sentence long: what customer data are we currently holding on the model provider's side. Everybody assumes somebody knows. Nobody does. The application logs what it sent and what came back, the provider stores the same thing independently, and when you go looking for the endpoint that lists what is stored there so you can answer the sentence, there is not one. Not for responses, not for conversations. There is no query. There is only whatever ids you happened to write down.

**Full guide with diagrams:** https://www.allanninal.dev/llm/stored-responses-accumulating/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_stored_state_probe.py
node node/openai-stored-state-probe.mjs
```

## Test it

```bash
pytest python/test_openai_stored_state_probe.py
node --test node/openai-stored-state-probe.test.mjs
```
