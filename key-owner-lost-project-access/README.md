# keys still work after their owner loses project access

She left in March. The laptop came back, SSO was switched off the same afternoon, and the offboarding ticket was closed with every box ticked. The API key she minted in her second week is still in the environment of a nightly job, still authenticating, still billing. Nothing revoked it, because nothing was ever asked to: the key is a separate object from her membership, and removing the membership left the key exactly as it was.

**Full guide with diagrams:** https://www.allanninal.dev/llm/key-owner-lost-project-access/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_orphaned_key_audit.py
node node/openai-orphaned-key-audit.mjs
```

## Test it

```bash
pytest python/test_openai_orphaned_key_audit.py
node --test node/openai-orphaned-key-audit.test.mjs
```
