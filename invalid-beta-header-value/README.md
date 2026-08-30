# The anthropic-beta value that 400s, and the one that went GA

The pull request adds one header and it is nine characters different from the one in the documentation. Every request the service makes now returns 400, the message names the header, and it takes about four minutes to spot. That is the easy half. The hard half is the header three services along that is spelled perfectly, was correct when it was written, returns 200 today, and is quietly holding that client on a response shape the platform stopped documenting in August &mdash; no error, no warning, and a list endpoint that has been missing expires_at for months.

**Full guide with diagrams:** https://www.allanninal.dev/llm/invalid-beta-header-value/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_beta_header_audit.py
node node/anthropic-beta-header-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_beta_header_audit.py
node --test node/anthropic-beta-header-audit.test.mjs
```
