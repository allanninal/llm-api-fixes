# Nobody has ever read the key lifecycle audit log

The control exists. It has existed since the organization was created, it has recorded every key minted and every key deleted and every member added since then, and it is complete, accurate and correctly timestamped. It has also never been read by anybody, because reading it requires standing up a job, and a log that is silent when everything is healthy gives nobody a reason to build one. There is an entry in there from a Tuesday in March: a key created at 02:14 UTC by an email address that is no longer on the roster. It has been sitting there for five months.

**Full guide with diagrams:** https://www.allanninal.dev/llm/unreviewed-key-lifecycle-in-audit-log/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/llm_key_lifecycle_review.py
node node/llm-key-lifecycle-review.mjs
```

## Test it

```bash
pytest python/test_llm_key_lifecycle_review.py
node --test node/llm-key-lifecycle-review.test.mjs
```
