# Parallel tool calls void the strict schema guarantee

The schemas are strict, every one of them, because somebody read the Structured Outputs page properly and did the work. The parser has no try/except around it, deliberately: the arguments are guaranteed to conform, so a failure there should be loud. It has been loud four times in three months, always on a Tuesday afternoon, always with a stack trace that says a required field was missing, and always unreproducible from the same prompt. The four turns have one thing in common that nobody looked at: each of them called two tools instead of one.

**Full guide with diagrams:** https://www.allanninal.dev/llm/parallel-tool-calls-with-strict-schema/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_parallel_strict_calls.py
node node/openai-parallel-strict-calls.mjs
```

## Test it

```bash
pytest python/test_openai_parallel_strict_calls.py
node --test node/openai-parallel-strict-calls.test.mjs
```
