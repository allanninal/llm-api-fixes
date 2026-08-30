# streamed responses report no usage and the dashboard undercounts

The internal cost dashboard has been trusted for a year. It reads the usage object off every response, sums it per project, multiplies by the price card and draws a line. In January the line and the invoice were within a few percent of each other. They are not now, and the gap is a third. Nothing in the dashboard is broken, and nothing in the pipeline dropped a record: the chat endpoint was switched to streaming in March, and a streamed chunk carries usage: null.

**Full guide with diagrams:** https://www.allanninal.dev/llm/streaming-usage-lost/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_streaming_usage_gap.py
node node/openai-streaming-usage-gap.mjs
```

## Test it

```bash
pytest python/test_openai_streaming_usage_gap.py
node --test node/openai-streaming-usage-gap.test.mjs
```
