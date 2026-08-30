# A project or workspace ceiling set below the org limit

The staging project was created for isolation, which is the advice everybody gives, and somebody set its rate limit low on the way past because staging does not need much. Eleven months later that project id is in the production deployment, because reusing it was one line and creating a new one was a ticket. Production now 429s at a fifth of the traffic the organization is entitled to, the organization dashboard shows plenty of headroom, and every other team is fine.

**Full guide with diagrams:** https://www.allanninal.dev/llm/project-rate-limit-below-org/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/rate_limit_below_org_audit.py
node node/rate-limit-below-org-audit.mjs
```

## Test it

```bash
pytest python/test_rate_limit_below_org_audit.py
node --test node/rate-limit-below-org-audit.test.mjs
```
