# Files failed to index and file_search quietly returns less

Somebody in support says the assistant does not know about the September pricing change, and you know for a fact that the September pricing memo was in the ingest. You check, and it was: the upload succeeded, the attach call returned 200, the ingest job logged 812 files indexed and exited zero. The store's status says completed. Every one of those things is true, and the memo is a scanned PDF with no text layer, so it is not in the index and never was.

**Full guide with diagrams:** https://www.allanninal.dev/llm/vector-store-file-attach-failed/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_vector_store_attach_failures.py
node node/openai-vector-store-attach-failures.mjs
```

## Test it

```bash
pytest python/test_openai_vector_store_attach_failures.py
node --test node/openai-vector-store-attach-failures.test.mjs
```
