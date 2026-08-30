# Claude Code edits rejected more often than they are kept

The diff comes up, it is forty lines, it is confidently wrong about where the validation lives, and it gets rejected in about two seconds. That happens eleven times before lunch, and none of those eleven rejections is recorded anywhere a person would look, because rejecting a proposal is not an error and not a failure and not, from the tool's point of view, an event worth complaining about. Every one of those forty-line diffs was generated at frontier output rates and paid for in full before anybody read it.

**Full guide with diagrams:** https://www.allanninal.dev/llm/claude-code-edit-rejection-rate-high/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/claude_code_edit_acceptance.py
node node/claude-code-edit-acceptance.mjs
```

## Test it

```bash
pytest python/test_claude_code_edit_acceptance.py
node --test node/claude-code-edit-acceptance.test.mjs
```
