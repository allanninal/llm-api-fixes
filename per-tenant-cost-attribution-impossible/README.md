# per-customer cost is unknowable because tenants share a key

Finance asks what the largest account costs to serve. It is a reasonable question and somebody says it will take an afternoon, because the Usage API has a group_by=user_id and the application has been sending a user field on every request since the first week. The afternoon produces a table with eleven rows in it. Nine are engineers, two are service accounts, and not one of them is a customer.

**Full guide with diagrams:** https://www.allanninal.dev/llm/per-tenant-cost-attribution-impossible/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_tenant_attribution_audit.py
node node/openai-tenant-attribution-audit.mjs
```

## Test it

```bash
pytest python/test_openai_tenant_attribution_audit.py
node --test node/openai-tenant-attribution-audit.test.mjs
```
