# prompt caching was never switched on anywhere

The system prompt is four thousand tokens of instructions, a tool catalogue and two worked examples. It is identical on every call, and there are three hundred thousand calls a month. It has been reprocessed at the full input rate every single time, because prompt caching is opt-in and nobody opted in. There is no error to find, no warning header, and no line in the invoice that says what this cost. There is only a field in the usage report that has been zero since the day the integration shipped.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompt-caching-never-used/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_prompt_cache_off.py
node node/anthropic-prompt-cache-off.mjs
```

## Test it

```bash
pytest python/test_anthropic_prompt_cache_off.py
node --test node/anthropic-prompt-cache-off.test.mjs
```
