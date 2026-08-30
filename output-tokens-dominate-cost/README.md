# output tokens, not input, are what the bill is made of

Every conversation about LLM cost starts with the prompt. The system prompt gets trimmed, the few-shot examples get cut, someone measures the context window. Then you group the cost report by token_type and find that three-quarters of the money is on the other side of the request, where none of the levers you just pulled reach.

**Full guide with diagrams:** https://www.allanninal.dev/llm/output-tokens-dominate-cost/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_output_cost_audit.py
node node/anthropic-output-cost-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_output_cost_audit.py
node --test node/anthropic-output-cost-audit.test.mjs
```
