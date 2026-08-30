# API keys that no request has ever used

Nobody left. Nobody was removed from anything. The engineer who minted the key in March is sitting eight feet away, still employed, still on the project, and could tell you within a second why she made it: a vendor evaluation that went nowhere. The key has authenticated exactly zero requests in the five months since, and it has full access to everything in that project, and there is no report anywhere that will ever mention it, because a credential with no traffic produces no cost line, no log entry and no alert. Creating it took one click. Deleting it requires somebody to be confident nothing breaks.

**Full guide with diagrams:** https://www.allanninal.dev/llm/api-key-never-used/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/llm_idle_key_audit.py
node node/llm-idle-key-audit.mjs
```

## Test it

```bash
pytest python/test_llm_idle_key_audit.py
node --test node/llm-idle-key-audit.test.mjs
```
