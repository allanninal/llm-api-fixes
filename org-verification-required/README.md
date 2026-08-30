# Model visible, streaming refused: the org is unverified

The nightly summarisation job is fine. The evaluation suite is fine. CI is fine. The chat panel in the product returns nothing at all, and has since the release that switched it to the newer model, and the only thing anyone can say about it is that it used to work. The model id is right &mdash; you checked, it resolves. The key is right &mdash; it is the same key the job uses. The difference between the route that works and the route that does not is one field in the request body, "stream": true, and the reason it fails has nothing to do with your code at all.

**Full guide with diagrams:** https://www.allanninal.dev/llm/org-verification-required/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_streaming_verification_probe.py
node node/openai-streaming-verification-probe.mjs
```

## Test it

```bash
pytest python/test_openai_streaming_verification_probe.py
node --test node/openai-streaming-verification-probe.test.mjs
```
