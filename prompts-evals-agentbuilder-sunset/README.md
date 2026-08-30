# Prompts, Evals and Agent Builder close: export, not rewrite

The deprecation notice reads like the others and gets triaged like the others: three surfaces, one date, put it in the sprint after next. What is different does not appear anywhere in the notice. The reusable prompt referenced as pmpt_a1b2 in four services is four characters of your source code and several hundred words of somebody else's, and those words are on OpenAI's side of the line. On 1 December the code still compiles, the deploy still succeeds, and the prompt is not in the repository because it never was.

**Full guide with diagrams:** https://www.allanninal.dev/llm/prompts-evals-agentbuilder-sunset/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/sunset_export_audit.py
node node/sunset-export-audit.mjs
```

## Test it

```bash
pytest python/test_sunset_export_audit.py
node --test node/sunset-export-audit.test.mjs
```
