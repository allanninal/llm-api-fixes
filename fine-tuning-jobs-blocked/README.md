# Fine-tuning stops taking new jobs while old ones keep serving

Nothing is broken, which is the problem. The classifier fine-tune is serving traffic, the job list is full of green, and the plan &mdash; retrain it in the new year when there is time &mdash; sounds entirely reasonable to everybody in the room. It is not reasonable, and the reason is not on any dashboard: nobody has run inference against a fine-tuned model in about nine weeks, because the classifier was quietly replaced by a prompt in June and only the old batch job still calls it. The right to create a fine-tuning job expired somewhere in there. There was no notification, because nothing happened.

**Full guide with diagrams:** https://www.allanninal.dev/llm/fine-tuning-jobs-blocked/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/fine_tuning_gate_audit.py
node node/fine-tuning-gate-audit.mjs
```

## Test it

```bash
pytest python/test_fine_tuning_gate_audit.py
node --test node/fine-tuning-gate-audit.test.mjs
```
