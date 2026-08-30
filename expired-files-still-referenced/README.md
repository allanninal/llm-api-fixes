# An expired file still answers metadata and fails every use

The document pipeline has been fine for months and this morning a batch of requests started failing on a handful of customers. Not all of them, and not consistently. The error is a 404, which sends everybody looking for a typo, and the id is right &mdash; you can paste it into a metadata call and get a filename, a size and a created date back. The file exists. It answers. And every request that tries to actually use it fails before inference, because what came back was the label and the thing behind it went away eleven days ago.

**Full guide with diagrams:** https://www.allanninal.dev/llm/expired-files-still-referenced/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_expired_file_refs.py
node node/anthropic-expired-file-refs.mjs
```

## Test it

```bash
pytest python/test_anthropic_expired_file_refs.py
node --test node/anthropic-expired-file-refs.test.mjs
```
