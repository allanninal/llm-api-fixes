# output tokens per minute is the real ceiling, not RPM

The capacity plan is a spreadsheet with requests per minute in it, and it has been right about everything for a year. Then thinking gets turned up on the summariser, and the same number of calls starts 429ing. Nobody changed the request rate. Nobody changed the prompts. What changed is that each answer got four times longer, and the limiter that was never in the spreadsheet is the one counting characters as they come out.

**Full guide with diagrams:** https://www.allanninal.dev/llm/otpm-exhausted/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_otpm_ceiling.py
node node/anthropic-otpm-ceiling.mjs
```

## Test it

```bash
pytest python/test_anthropic_otpm_ceiling.py
node --test node/anthropic-otpm-ceiling.test.mjs
```
