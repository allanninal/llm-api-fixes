# Vector store bytes grow while nobody queries the index

Somebody finally asks about the small line. It has been on the invoice for fourteen months, it has never been more than a couple of hundred dollars, and it is the only line that has gone up every single month regardless of what shipped. It is vector store storage. It is billed on bytes retained per hour rather than on anything anybody did, and a good deal of it is a corpus indexed for a demo in the spring of last year that has not been searched since the demo.

**Full guide with diagrams:** https://www.allanninal.dev/llm/vector-store-storage-cost-creeping/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_vector_store_storage_trend.py
node node/openai-vector-store-storage-trend.mjs
```

## Test it

```bash
pytest python/test_openai_vector_store_storage_trend.py
node --test node/openai-vector-store-storage-trend.test.mjs
```
