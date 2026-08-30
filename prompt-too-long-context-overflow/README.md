# Prompts overflow the context window and 400 as too long

The retrieval step got better last quarter, so it returns eight chunks instead of five. The agent loop got longer, because agents do. The tool list grew by four definitions nobody costed. None of those three changes touched the prompt template, and none of them was reviewed as a capacity change, and one afternoon the request that has worked for a year comes back 400 with prompt is too long.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompt-too-long-context-overflow/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_context_preflight.py
node node/anthropic-context-preflight.mjs
```

## Test it

```bash
pytest python/test_anthropic_context_preflight.py
node --test node/anthropic-context-preflight.test.mjs
```
