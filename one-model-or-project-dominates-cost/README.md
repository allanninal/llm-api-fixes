# one line item or project is most of the organization's bill

The bill is roughly what everyone expected, which is why nobody has opened it. When somebody finally does, one line item is seventy-eight percent of it, and it is not the one the team spent last quarter optimising. There is no error here and nothing failed. There is a month of engineering time that went into a row worth three percent of the total, because the report was read as a single number and single numbers do not have a shape.

**Full guide with diagrams:** https://www.allanninal.dev/llm/one-model-or-project-dominates-cost/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_cost_concentration_audit.py
node node/openai-cost-concentration-audit.mjs
```

## Test it

```bash
pytest python/test_openai_cost_concentration_audit.py
node --test node/openai-cost-concentration-audit.test.mjs
```
