# a fine-tuned model was trained, billed, and never called once

There was a quarter when fine-tuning was going to be the answer. Four jobs were queued over three weeks, each on a slightly better training file, and the last one came back with a loss curve somebody screenshotted into Slack. Then the base model got better, or the prompt got better, or the person who cared moved teams. The model ids are still there. They still resolve. They have between them served zero requests, and the training was invoiced the month it ran.

**Full guide with diagrams:** https://www.allanninal.dev/llm/fine-tuned-model-never-used/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_fine_tune_usage_audit.py
node node/openai-fine-tune-usage-audit.mjs
```

## Test it

```bash
pytest python/test_openai_fine_tune_usage_audit.py
node --test node/openai-fine-tune-usage-audit.test.mjs
```
