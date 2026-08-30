# fast mode billed at twice the rate and served as default

Somebody turned on Fast mode for the checkout assistant eight months ago, in the console, in an afternoon nobody wrote down. The latency graph looked better for a week. It does not look better now, and the team has spent two sprints on the retrieval step trying to work out why. The requests still carry the premium tier, the responses still return 200, and the field that says which tier actually served them is not the field anyone is logging.

**Full guide with diagrams:** https://www.allanninal.dev/llm/fast-mode-silently-downgraded/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_fast_mode_tier_audit.py
node node/openai-fast-mode-tier-audit.mjs
```

## Test it

```bash
pytest python/test_openai_fast_mode_tier_audit.py
node --test node/openai-fast-mode-tier-audit.test.mjs
```
