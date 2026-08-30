# The Videos API closes and no successor model is listed

The migration ticket is written before anybody reads the table properly, because everyone has done this before: find the retired id, look up the successor, change the string, ship. So the ticket says "migrate off sora-2" and it is assigned and estimated. Then somebody opens the deprecations page to fill in the target and finds the replacement column is empty. Not to be announced, not see the migration guide. Empty, for all five ids, because the thing being removed is not a model. It is the feature.

**Full guide with diagrams:** https://www.allanninal.dev/llm/sora-videos-api-no-replacement/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/sora_shutdown_inventory.py
node node/sora-shutdown-inventory.mjs
```

## Test it

```bash
pytest python/test_sora_shutdown_inventory.py
node --test node/sora-shutdown-inventory.test.mjs
```
