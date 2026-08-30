# max_tokens is set above the model's own output cap

The classifier was moved onto the cheap model on a Friday, which is the correct thing to do with a classifier. It is the same helper function everything else uses, so it inherited the same max_tokens, which is a number somebody picked for the model that writes reports. Every call to it now comes back 400, and the message says exactly what is wrong, and the message is in a log that no dashboard reads.

**Full guide with diagrams:** https://www.allanninal.dev/llm/max-tokens-above-model-cap/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_max_tokens_cap.py
node node/anthropic-max-tokens-cap.mjs
```

## Test it

```bash
pytest python/test_anthropic_max_tokens_cap.py
node --test node/anthropic-max-tokens-cap.test.mjs
```
