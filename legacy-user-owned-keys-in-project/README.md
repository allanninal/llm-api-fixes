# Production keys owned by people, not service accounts

The service has been up for two years and it has never once paged anybody about credentials. That is because the credential is Marco's. He minted it in the first week, before the project structure existed, because a personal key works the instant you create it and a service account is a thing you have to think about first. Nothing has gone wrong. Marco is still here, still on the team, still the person who would fix it. The only fact that has changed in two years is that eleven thousand dollars a month now moves through a credential whose lifecycle is attached to one person's employment rather than to the service's.

**Full guide with diagrams:** https://www.allanninal.dev/llm/legacy-user-owned-keys-in-project/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_user_owned_key_audit.py
node node/openai-user-owned-key-audit.mjs
```

## Test it

```bash
pytest python/test_openai_user_owned_key_audit.py
node --test node/openai-user-owned-key-audit.test.mjs
```
