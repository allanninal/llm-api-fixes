# Nothing has ever called the free moderation endpoint

The product takes free text from anyone with the link and has done since launch. Nobody thinks of it as a moderation problem, because nothing has gone wrong yet and the model refuses most of what it should refuse on its own. Then a support ticket arrives with a screenshot, and somebody asks the only question that matters in the first ten minutes: what do we already screen? The honest answer takes an afternoon to establish, and it turns out to be nothing &mdash; not because anyone decided against it, but because moderation is a separate endpoint you have to go and call, and no line of code ever did.

**Full guide with diagrams:** https://www.allanninal.dev/llm/moderation-never-called/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_moderation_coverage_audit.py
node node/openai-moderation-coverage-audit.mjs
```

## Test it

```bash
pytest python/test_openai_moderation_coverage_audit.py
node --test node/openai-moderation-coverage-audit.test.mjs
```
