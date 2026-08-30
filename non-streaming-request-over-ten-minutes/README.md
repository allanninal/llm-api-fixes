# A non-streaming request over 10 minutes times out with 504

The prompt is two thousand tokens. The context window is nowhere near full. Nothing is too large by any measure anybody in the room has checked, and the request still dies &mdash; sometimes as 504 with timeout_error, more often as nothing at all, because a load balancer somewhere between you and Anthropic closed an idle connection while the model was still writing. The ceiling this hit is a clock.

**Full guide with diagrams:** https://www.allanninal.dev/llm/non-streaming-request-over-ten-minutes/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_wall_clock_preflight.py
node node/anthropic-wall-clock-preflight.mjs
```

## Test it

```bash
pytest python/test_anthropic_wall_clock_preflight.py
node --test node/anthropic-wall-clock-preflight.test.mjs
```
