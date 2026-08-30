# audio and image usage never shows up in a token dashboard

The internal dashboard has been within a few percent of the invoice for a year, and a few percent is what everyone expects a dashboard to be. It is not rounding. It is the text-to-speech in the mobile app, the transcription on the support calls, the thumbnails the marketing tool generates, and the web search the agent does before it answers. None of those are denominated in tokens, and the endpoint the dashboard was built on only knows about tokens.

**Full guide with diagrams:** https://www.allanninal.dev/llm/audio-and-image-line-items-unnoticed/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_modality_spend_reconcile.py
node node/openai-modality-spend-reconcile.mjs
```

## Test it

```bash
pytest python/test_openai_modality_spend_reconcile.py
node --test node/openai-modality-spend-reconcile.test.mjs
```
