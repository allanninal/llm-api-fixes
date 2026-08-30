# The gateway strips the header your backoff depends on

The backoff was reviewed, it is textbook, and it reads retry-after before it sleeps. It has also never once seen that header, because the reverse proxy in front of the API forwards a response header allowlist that somebody wrote in 2023 and it contains content-type. So the handler falls through to its default of one second, retries into a bucket that has not refilled, and every retry pushes the reset further out while the incident channel fills up with people saying the backoff must be broken.

**Full guide with diagrams:** https://www.allanninal.dev/llm/retry-after-header-ignored/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/retry_after_header_probe.py
node node/retry-after-header-probe.mjs
```

## Test it

```bash
pytest python/test_retry_after_header_probe.py
node --test node/retry-after-header-probe.test.mjs
```
