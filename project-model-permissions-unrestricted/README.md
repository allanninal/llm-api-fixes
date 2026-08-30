# A model permission policy that has never excluded anything

The postmortem is short and everybody already knows how it ends. A nightly classification job that labels support tickets was pointed at the most capable model in the catalogue, because that is what the developer had in their editor from the last thing they built, and it ran that way for five weeks. The interesting question is not why they chose it. It is the one somebody asks at the end of the meeting, almost as an aside: what would have stopped it? And the answer is nothing, in that project or any other, because model access is open by default and the control that closes it is per project and opt-in.

**Full guide with diagrams:** https://www.allanninal.dev/llm/project-model-permissions-unrestricted/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_project_model_policy_audit.py
node node/openai-project-model-policy-audit.mjs
```

## Test it

```bash
pytest python/test_openai_project_model_policy_audit.py
node --test node/openai-project-model-policy-audit.test.mjs
```
