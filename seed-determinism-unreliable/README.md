# system_fingerprint moved and seed stopped reproducing

The golden-file suite has been green for eight months. It pins seed, it pins temperature to zero, it asserts on the exact string the model returned the day the fixtures were recorded, and everybody agreed at the time that this was a reasonable way to test an LLM because it worked. This morning forty of them fail. The diffs are trivial &mdash; a comma, a reordered clause, one adjective &mdash; and nothing in the repository changed. The commit that broke them is not in your repository.

**Full guide with diagrams:** https://www.allanninal.dev/llm/seed-determinism-unreliable/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_fingerprint_drift.py
node node/openai-fingerprint-drift.mjs
```

## Test it

```bash
pytest python/test_openai_fingerprint_drift.py
node --test node/openai-fingerprint-drift.test.mjs
```
