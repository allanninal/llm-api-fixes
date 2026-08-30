# The same body counts 30% more tokens on the newer model

The migration went fine. The evaluations were better, the latency was acceptable, nothing 500ed, and the rollout took an afternoon. Three weeks later the finance channel asks why input spend is up by a third on flat traffic, and somebody else opens a ticket about retrieval quality, and a third person notices that the conversation compactor now triggers two turns earlier than it used to. None of these is a bug. They are all the same fact arriving in three different rooms: the number your code uses to mean how big is this was measured against a model you no longer call.

**Full guide with diagrams:** https://www.allanninal.dev/llm/token-counts-reused-across-tokenizers/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_tokenizer_delta.py
node node/anthropic-tokenizer-delta.mjs
```

## Test it

```bash
pytest python/test_anthropic_tokenizer_delta.py
node --test node/anthropic-tokenizer-delta.test.mjs
```
