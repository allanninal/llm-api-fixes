# One project holds every environment, so nothing can be capped

Somebody asks what production costs. Not the API bill, which everyone knows to the dollar, but the production share of it, and the answer takes four days to arrive because there is no answer. Every request the company has ever made to OpenAI &mdash; the customer-facing assistant, the nightly evaluation suite, the CI job that regenerates fixtures, eleven laptops, and the prototype somebody wrote in March and never turned off &mdash; landed in a project called Default project that was created before anybody had an opinion about structure.

**Full guide with diagrams:** https://www.allanninal.dev/llm/no-prod-dev-project-separation/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_project_boundary_audit.py
node node/openai-project-boundary-audit.mjs
```

## Test it

```bash
pytest python/test_openai_project_boundary_audit.py
node --test node/openai-project-boundary-audit.test.mjs
```
