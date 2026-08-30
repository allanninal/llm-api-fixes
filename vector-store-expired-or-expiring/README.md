# A vector store with expires_after deletes itself on a clock

The retrieval demo was built in one good afternoon in March, shown twice, and then left alone while the team shipped something else. In May somebody asks to show it again, it comes up, and it answers every question out of the model's own head. The store id is unchanged and still in the config. The store still exists. Its status is expired, its file_counts are all zero, and the file objects it held were deleted on a schedule that was set at creation by a tool nobody remembers configuring.

**Full guide with diagrams:** https://www.allanninal.dev/llm/vector-store-expired-or-expiring/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_vector_store_expiry_audit.py
node node/openai-vector-store-expiry-audit.mjs
```

## Test it

```bash
pytest python/test_openai_vector_store_expiry_audit.py
node --test node/openai-vector-store-expiry-audit.test.mjs
```
