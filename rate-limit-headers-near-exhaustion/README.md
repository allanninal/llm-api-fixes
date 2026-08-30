# x-ratelimit-remaining sits near zero before any 429

Nothing has failed. There is no incident, no 429 in the logs, no page. There is a number that says you are running on four percent of your token budget at four in the afternoon, and it arrives attached to every successful response your application has ever received, and no code you own has ever looked at it.

**Full guide with diagrams:** https://www.allanninal.dev/llm/rate-limit-headers-near-exhaustion/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_rate_limit_headroom.py
node node/openai-rate-limit-headroom.mjs
```

## Test it

```bash
pytest python/test_openai_rate_limit_headroom.py
node --test node/openai-rate-limit-headroom.test.mjs
```
