# Files pile up against a storage ceiling nothing reports

The batch pipeline has run every night for two years and tonight it stops on the first step, uploading the input file, with an error nobody has seen before. Nothing was deployed. The key works, because the same key just listed a thousand files without complaint. Reads are fine. Retrieval is fine. Inference is fine. The only thing that is broken is writing, and it is broken because a number you have never once looked at reached a ceiling you have never once been shown.

**Full guide with diagrams:** https://www.allanninal.dev/llm/files-accumulating-against-storage-quota/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/file_store_quota_audit.py
node node/file-store-quota-audit.mjs
```

## Test it

```bash
pytest python/test_file_store_quota_audit.py
node --test node/file-store-quota-audit.test.mjs
```
