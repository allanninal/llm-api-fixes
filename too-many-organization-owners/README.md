# Almost everyone in the organization holds the owner role

Nobody decided this. Someone needed to create a project in week three and the fastest unblock was to make them an owner; someone needed to raise a rate limit in month five and the same thing happened; the contractor needed a key for a fortnight in March. Every one of those was the right call at the time and none of them was ever undone, because demotion is a visible act with a social cost and nothing in the platform ever asks. Two years later the roster has fourteen names on it and thirteen can change the billing settings.

**Full guide with diagrams:** https://www.allanninal.dev/llm/too-many-organization-owners/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_owner_ratio_audit.py
node node/openai-owner-ratio-audit.mjs
```

## Test it

```bash
pytest python/test_openai_owner_ratio_audit.py
node --test node/openai-owner-ratio-audit.test.mjs
```
