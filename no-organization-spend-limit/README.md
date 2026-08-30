# no hard spend limit is set, so the bill has no ceiling

The console shows a budget chart, and everybody who has looked at it believes it is a brake. It is not; it is a chart. The hard limit is a separate object on a separate admin endpoint, it is off by default, and until somebody turns it on there is no amount of spend in a month that will cause a request to be refused.

**Full guide with diagrams:** https://www.allanninal.dev/llm/no-organization-spend-limit/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_spend_limit_audit.py
node node/openai-spend-limit-audit.mjs
```

## Test it

```bash
pytest python/test_openai_spend_limit_audit.py
node --test node/openai-spend-limit-audit.test.mjs
```
