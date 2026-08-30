# The model refused and the refusal field was never read

The support summariser stopped producing summaries for a particular kind of ticket. Not all of them, and not with an error: the row was written, the summary column held an empty string, and the dashboard that counts summaries counted them. It took a week and a customer complaint before anyone read a stored response end to end, and there it was, in a content item nobody's code had ever touched: the model had declined, politely, and said why.

**Full guide with diagrams:** https://www.allanninal.dev/llm/refusal-field-ignored/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_refusal_channel.py
node node/openai-refusal-channel.mjs
```

## Test it

```bash
pytest python/test_openai_refusal_channel.py
node --test node/openai-refusal-channel.test.mjs
```
