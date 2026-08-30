# Tool shipped on every request and never once called

The tool registry has twenty-six entries. Nineteen of them were written in the same fortnight by four people, and the descriptions read like function signatures because that is what they were copied from. Every one of them goes out on every request, because the registry is built once at start-up and handed to the client whole. Nothing is broken. The handlers for six of those tools have not been entered in production since the day they were merged, and the tools are still being paid for on every turn.

**Full guide with diagrams:** https://www.allanninal.dev/llm/tool-defined-but-never-called/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_dead_tool_definitions.py
node node/openai-dead-tool-definitions.mjs
```

## Test it

```bash
pytest python/test_openai_dead_tool_definitions.py
node --test node/openai-dead-tool-definitions.test.mjs
```
