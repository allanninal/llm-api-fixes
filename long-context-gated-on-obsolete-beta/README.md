# The 1M context window is capped at 200k in your own code

The long-context work took a quarter. There is a constant called MAX_CONTEXT_TOKENS, there is a guard that truncates the retrieved documents before they reach it, there is a branch that routes anything over the line to a summarise-first path, and there is a comment above all of it citing a beta header. Every one of those was correct when it was written. The model ids in the config have changed four times since, the window they now report is a million tokens, and the constant still says two hundred thousand, so the truncation still fires, the summarise path still runs, and the capability the company is paying for stops at the same place it stopped eighteen months ago.

**Full guide with diagrams:** https://www.allanninal.dev/llm/long-context-gated-on-obsolete-beta/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_context_window_cap.py
node node/anthropic-context-window-cap.mjs
```

## Test it

```bash
pytest python/test_anthropic_context_window_cap.py
node --test node/anthropic-context-window-cap.test.mjs
```
