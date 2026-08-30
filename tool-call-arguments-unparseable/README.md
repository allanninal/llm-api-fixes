# Tool-call arguments that parse and still break the schema

The agent had been running for a month when a run started dying in the middle of a turn, roughly once in three hundred calls, always on the same tool. The traceback was a KeyError inside the handler, four frames below anything that knew about HTTP. The arguments string had parsed without complaint. It was well-formed JSON, it was even plausible JSON, and it described a call to a function whose signature had changed in a pull request the tool schema had not followed.

**Full guide with diagrams:** https://www.allanninal.dev/llm/tool-call-arguments-unparseable/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_tool_call_arguments.py
node node/openai-tool-call-arguments.mjs
```

## Test it

```bash
pytest python/test_openai_tool_call_arguments.py
node --test node/openai-tool-call-arguments.test.mjs
```
