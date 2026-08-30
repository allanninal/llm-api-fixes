# previous_response_id 404s once the parent has aged out

The support assistant remembers everything, which is the entire product. A customer opens a thread in March, adds to it in April, and comes back in June to ask about the thing they described the first time. This time the request 404s. Not the model, not the key, not the prompt: the id of the message before this one, which your code has been passing forward faithfully for three months. Server-side conversation state is a convenience with an expiry date on it, and nobody read the date.

**Full guide with diagrams:** https://www.allanninal.dev/llm/previous-response-id-chain-broken/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_response_chain_probe.py
node node/openai-response-chain-probe.mjs
```

## Test it

```bash
pytest python/test_openai_response_chain_probe.py
node --test node/openai-response-chain-probe.test.mjs
```
