# US inference geo is billing every token at 1.1x

Somebody in a procurement call last spring said that the data has to stay in the US, and somebody else, being helpful, went and set the workspace default that afternoon. Nobody wrote it down, because it took four seconds. The contract that prompted it was signed for one customer. The workspace serves all of them, and every token any of them has generated since has been billed at one and a tenth times the rate card.

**Full guide with diagrams:** https://www.allanninal.dev/llm/us-inference-geo-premium-unnoticed/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_inference_geo_premium_audit.py
node node/anthropic-inference-geo-premium-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_inference_geo_premium_audit.py
node --test node/anthropic-inference-geo-premium-audit.test.mjs
```
