# A CMEK key config is inert but assumed to be encrypting

The answer in the security questionnaire is a single sentence and it took three weeks of work to be able to write it: customer data is encrypted at rest under a key we control, in our own KMS, which we can revoke. The engineer who did that work created the key, registered it, and moved on to the next ticket, because from the outside everything looked done &mdash; the config was there, it had the right ARN, the console showed it. What nobody did was the second step, and there is no error anywhere that says so, because an encryption config that is not attached to anything does not fail. It just is not used.

**Full guide with diagrams:** https://www.allanninal.dev/llm/external-key-config-unattached/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_cmek_external_key_audit.py
node node/anthropic-cmek-external-key-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_cmek_external_key_audit.py
node --test node/anthropic-cmek-external-key-audit.test.mjs
```
