# code execution has spent its free 1,550 container hours

The analytics route attaches the customer's CSV to the request, because sometimes the question is about the numbers in it and when it is, the model should be able to run something. Sometimes is about one call in forty. The other thirty-nine attach the file, spin up a container to hold it, ask something that needs no arithmetic at all, and are billed for five minutes of a machine that was never asked to do anything.

**Full guide with diagrams:** https://www.allanninal.dev/llm/code-execution-hours-exceed-free-allowance/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_code_execution_hours_audit.py
node node/anthropic-code-execution-hours-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_code_execution_hours_audit.py
node --test node/anthropic-code-execution-hours-audit.test.mjs
```
