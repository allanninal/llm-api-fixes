# reasoning tokens are billed as output but never returned

Somebody changed one model constant. The answers coming back are the same length as before, the prompts are the same length as before, the request count is flat &mdash; and the line for that model on the cost report went up by a factor of four. The tokens you are paying for were generated, billed at the output rate, and then not returned to you.

**Full guide with diagrams:** https://www.allanninal.dev/llm/reasoning-tokens-billed-invisibly/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_reasoning_token_audit.py
node node/openai-reasoning-token-audit.mjs
```

## Test it

```bash
pytest python/test_openai_reasoning_token_audit.py
node --test node/openai-reasoning-token-audit.test.mjs
```
