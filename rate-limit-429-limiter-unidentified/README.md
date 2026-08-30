# 429s are retried blindly without reading which limit hit

The handler is four lines long and it has been in production for a year. Catch the 429, sleep, retry, give up after three. It works, in the sense that the process does not crash. It has also never once told anybody which of three separate limiters emptied, because the answer was in the response headers and the handler caught an exception class instead of reading a response.

**Full guide with diagrams:** https://www.allanninal.dev/llm/rate-limit-429-limiter-unidentified/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_limiter_identify.py
node node/anthropic-limiter-identify.mjs
```

## Test it

```bash
pytest python/test_anthropic_limiter_identify.py
node --test node/anthropic-limiter-identify.test.mjs
```
