# A 32 MB request is rejected with 413 before Anthropic sees it

The PDF is 24 MB, which everybody agreed was fine, because the context window is 200,000 tokens and a 24 MB PDF is nothing like 200,000 tokens. The request comes back 413 anyway, with a body that does not look like Anthropic's usual error envelope, and nothing about it appears in the usage report. It never reached Anthropic. It was refused by a proxy in front of the API, for a reason that has nothing to do with tokens.

**Full guide with diagrams:** https://www.allanninal.dev/llm/request-too-large-413/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_request_bytes.py
node node/anthropic-request-bytes.mjs
```

## Test it

```bash
pytest python/test_anthropic_request_bytes.py
node --test node/anthropic-request-bytes.test.mjs
```
