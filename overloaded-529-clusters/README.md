# 529 overloaded errors arrive in clusters and get dropped

Three minutes on a Tuesday afternoon, and about fourteen hundred jobs are simply not in the output table. Nothing crashed. The worker's error counter shows a spike of something it logged as unexpected status, because the client special-cases 429 and 500 and lets everything else fall through to a generic failure path that drops the work. The status was 529, the platform was over capacity for four minutes, and the only trace left is that Anthropic did no work in those minutes while your client believed it was sending plenty.

**Full guide with diagrams:** https://www.allanninal.dev/llm/overloaded-529-clusters/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_overload_residual.py
node node/anthropic-overload-residual.mjs
```

## Test it

```bash
pytest python/test_anthropic_overload_residual.py
node --test node/anthropic-overload-residual.test.mjs
```
