# Organization invites sat pending until they expired

The new engineer said she was set up, and she was: the laptop arrived, SSO worked, the repository cloned. Six weeks later somebody notices that the nightly job is running under a colleague's personal key because she asked to borrow it on her second day and never stopped. The invite to the API organization was sent on her first morning, went to a filtered folder, and is still sitting in the list marked pending with an expires_at that passed in April.

**Full guide with diagrams:** https://www.allanninal.dev/llm/openai-invites-pending-past-expiry/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_stale_invite_audit.py
node node/openai-stale-invite-audit.mjs
```

## Test it

```bash
pytest python/test_openai_stale_invite_audit.py
node --test node/openai-stale-invite-audit.test.mjs
```
