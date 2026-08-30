# Claude Code sessions billed with zero cache reads

Two developers on the same team, the same repository, the same model, and one of them costs four times what the other does. The expensive one is not doing more work &mdash; fewer commits, in fact &mdash; and the cheap one has not configured anything. The difference is a habit: one of them keeps a session open and talks to it, and the other opens a fresh session for every question, which feels tidier and means the project context, the tool definitions and the file contents are paid for again at full rate every single time.

**Full guide with diagrams:** https://www.allanninal.dev/llm/claude-code-sessions-not-hitting-cache/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/claude_code_cache_coverage.py
node node/claude-code-cache-coverage.mjs
```

## Test it

```bash
pytest python/test_claude_code_cache_coverage.py
node --test node/claude-code-cache-coverage.test.mjs
```
