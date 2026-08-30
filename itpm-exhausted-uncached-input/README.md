# ITPM runs out because uncached input is never cached

The 429s arrive in the afternoon and the team does what teams do: fewer workers, longer sleeps, a queue in front. None of it moves. The request counter was never the thing that ran out. What ran out was input tokens per minute, and the reason is that the same forty thousand tokens of system prompt and tool schemas are sent uncached on every single call, and every one of those tokens is charged against the limiter.

**Full guide with diagrams:** https://www.allanninal.dev/llm/itpm-exhausted-uncached-input/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_itpm_headroom.py
node node/anthropic-itpm-headroom.mjs
```

## Test it

```bash
pytest python/test_anthropic_itpm_headroom.py
node --test node/anthropic-itpm-headroom.test.mjs
```
