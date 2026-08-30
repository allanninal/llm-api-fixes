# JSON cut off mid-object because the ceiling was reached

The extraction pipeline had been green for six weeks and then started dropping about one document in forty into the dead-letter queue with a JSONDecodeError. The traceback pointed at a worker three hops from any HTTP client, so the first day went on the queue and the second on the worker. What it turned out to be was a 200. The model had followed the schema exactly, the way strict mode promises, and had been cut off halfway through the fourth line item of an unusually long invoice. Everything about that request succeeded, including the bill.

**Full guide with diagrams:** https://www.allanninal.dev/llm/structured-output-truncated-by-length/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_truncated_structured_output.py
node node/openai-truncated-structured-output.mjs
```

## Test it

```bash
pytest python/test_openai_truncated_structured_output.py
node --test node/openai-truncated-structured-output.test.mjs
```
