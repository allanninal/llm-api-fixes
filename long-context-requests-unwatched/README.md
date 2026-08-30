# most of your input tokens sit in the 200k-1M band

The agent keeps everything. That was the design: give it the whole ticket history, the whole document, every tool result it has ever produced in this session, and let it decide what matters. It worked beautifully in testing, where a session was four turns. In production a session is forty, each turn carries everything the previous thirty-nine produced, and the prefix has quietly grown to four hundred thousand tokens that get sent again from scratch every single time somebody types.

**Full guide with diagrams:** https://www.allanninal.dev/llm/long-context-requests-unwatched/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_long_context_audit.py
node node/anthropic-long-context-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_long_context_audit.py
node --test node/anthropic-long-context-audit.test.mjs
```
