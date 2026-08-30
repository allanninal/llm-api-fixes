# The fine-tuning job failed and error.code was never read

Someone kicked off the training run on a Thursday afternoon, watched the 200 come back, pasted the job id into the channel, and went home. The following Tuesday the deploy that was supposed to point at the new model is still pointing at the old one, which nobody notices because it works. Three weeks later a stakeholder asks how the fine-tune is performing and the honest answer turns out to be that it never trained. The job object has been sitting there the entire time with a status, an error code and the name of the field that was wrong, and nothing ever asked it.

**Full guide with diagrams:** https://www.allanninal.dev/llm/fine-tune-job-failed-with-error-code/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_fine_tune_failures.py
node node/openai-fine-tune-failures.mjs
```

## Test it

```bash
pytest python/test_openai_fine_tune_failures.py
node --test node/openai-fine-tune-failures.test.mjs
```
