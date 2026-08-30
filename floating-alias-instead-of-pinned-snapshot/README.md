# a floating model alias silently changes model under you

No error, no deploy, no incident. The evals were 91% on Thursday and 87% on Monday, the mean output length moved, the prompt cache hit rate dropped a few points and the bill went up slightly. Everyone looks at the prompt, which did not change, and at the retrieval corpus, which did not change either. The model changed: the config names an alias, and an alias is a pointer, not a model.

**Full guide with diagrams:** https://www.allanninal.dev/llm/floating-alias-instead-of-pinned-snapshot/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_alias_pinning_audit.py
node node/anthropic-alias-pinning-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_alias_pinning_audit.py
node --test node/anthropic-alias-pinning-audit.test.mjs
```
