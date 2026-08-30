# 429 credit_balance_exhausted retried forever as a rate limit

Traffic did not degrade, it stopped. Every request comes back 429, your retry wrapper does what it was built to do, and eight hours later it is still doing it. The status code says slow down. The code field inside the body says the account is out of money, and nothing in the SDK draws a line between the two &mdash; RateLimitError is raised for both.

**Full guide with diagrams:** https://www.allanninal.dev/llm/quota-exhausted-not-rate-limited/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_quota_wall_audit.py
node node/openai-quota-wall-audit.mjs
```

## Test it

```bash
pytest python/test_openai_quota_wall_audit.py
node --test node/openai-quota-wall-audit.test.mjs
```
