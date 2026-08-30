# Priority Tier never covered the model you migrated to

The commitment was signed two years ago, and the whole point of it was the 529s. It worked: the overload errors stopped, the on-call rotation got quiet, and everybody moved on. Then the platform team migrated the main assistant to a newer model, which was the correct thing to do on every axis they measured, and nothing in the migration checklist mentioned tiers because service_tier was already set to auto and had been for two years. The 529s came back four months later and nobody connected the two events, because there is no error, no warning and no line on the invoice that says the tier stopped applying.

**Full guide with diagrams:** https://www.allanninal.dev/llm/priority-tier-model-unsupported/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_priority_tier_coverage.py
node node/anthropic-priority-tier-coverage.mjs
```

## Test it

```bash
pytest python/test_anthropic_priority_tier_coverage.py
node --test node/anthropic-priority-tier-coverage.test.mjs
```
