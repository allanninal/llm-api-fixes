# An empty vector store is still named in vector_store_ids

The retrieval feature is live and nobody has complained, which is the part that should worry you. Every request carries a file_search tool with a vector_store_ids array copied out of the config months ago; every response comes back 200 with no citations attached; and the model, asked what the refund window is, says thirty days in a confident and well-structured paragraph. It is thirty days in the training data. Your policy changed to fourteen in March, it is in the document you indexed, and the store that was supposed to hold that document has nothing in it.

**Full guide with diagrams:** https://www.allanninal.dev/llm/empty-vector-store-still-referenced/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_empty_vector_store_audit.py
node node/openai-empty-vector-store-audit.mjs
```

## Test it

```bash
pytest python/test_openai_empty_vector_store_audit.py
node --test node/openai-empty-vector-store-audit.test.mjs
```
