# A service account key that has never been rotated

This one is the reward for having done it right. Somebody read the guidance, created service accounts, moved production onto them and deleted the personal keys, and every audit since has come back clean because every audit since has been looking for personal keys. The service account was created in February two years ago. Its key was minted the same afternoon. There has never been a second key, so there has never been a moment when two keys were valid at once, so rotation has never been a deploy with a rollback: it has always been a hard cutover with an outage on the other side of it. Which is why it keeps being scheduled for next quarter.

**Full guide with diagrams:** https://www.allanninal.dev/llm/service-account-key-never-rotated/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_key_rotation_clock.py
node node/openai-key-rotation-clock.mjs
```

## Test it

```bash
pytest python/test_openai_key_rotation_clock.py
node --test node/openai-key-rotation-clock.test.mjs
```
