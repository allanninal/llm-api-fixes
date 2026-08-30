# Tool schemas are most of the input tokens on every call

The agent has forty tools because forty things are worth doing, and each definition is a careful JSON schema with descriptions on every property because that is how you get the arguments right. A support turn is one sentence from a customer. Nobody has ever asked what the block in front of that sentence weighs, and the answer, when somebody finally counts it, is that the machinery is thirteen times the conversation on every single call.

**Full guide with diagrams:** https://www.allanninal.dev/llm/tool-schemas-dominate-input-tokens/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_tool_schema_overhead.py
node node/anthropic-tool-schema-overhead.mjs
```

## Test it

```bash
pytest python/test_anthropic_tool_schema_overhead.py
node --test node/anthropic-tool-schema-overhead.test.mjs
```
