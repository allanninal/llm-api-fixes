# 429s while every minute sits under the configured limit

The backfill went out at 09:00 and 429ed for eleven minutes. Somebody pulled the usage report afterwards, and the worst minute in the whole window used a fifth of the organization's input tokens per minute. Nobody believes the graph, so the ticket says rate limit increase required and the increase, when it arrives, changes nothing: the same job trips at the same point next Tuesday, still using a fifth of a limit that is now twice as big.

**Full guide with diagrams:** https://www.allanninal.dev/llm/acceleration-limit-on-traffic-spike/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_ramp_acceleration.py
node node/anthropic-ramp-acceleration.mjs
```

## Test it

```bash
pytest python/test_anthropic_ramp_acceleration.py
node --test node/anthropic-ramp-acceleration.test.mjs
```
