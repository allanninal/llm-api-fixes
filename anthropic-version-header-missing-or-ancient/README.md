# anthropic-version is missing, ancient, or added in transit

The webhook receiver has worked for a year. It is forty lines of fetch written the afternoon somebody needed it, it posts to Claude, and it has never once been touched. Today it returns 400 invalid_request_error on every call and the message is about a header you have never had to think about, because the SDK sets it and this thing is not the SDK. The part worth understanding is not the fix, which is one line. It is why it worked yesterday: the request used to go through the gateway, the gateway added anthropic-version for you, and last week somebody pointed that service straight at the API.

**Full guide with diagrams:** https://www.allanninal.dev/llm/anthropic-version-header-missing-or-ancient/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_version_header_probe.py
node node/anthropic-version-header-probe.mjs
```

## Test it

```bash
pytest python/test_anthropic_version_header_probe.py
node --test node/anthropic-version-header-probe.test.mjs
```
