# Cost lands in the default workspace and cannot be charged back

The chargeback spreadsheet has one row that never shrinks. Four workspaces are named after the teams that own them and add up to about sixty per cent of the bill; the rest arrives under a heading somebody typed once as Unallocated and has been carrying forward every month since. It is not a rounding error and it is not fraud. It is the default workspace, and it is reported as null, which is not a workspace you can go and talk to.

**Full guide with diagrams:** https://www.allanninal.dev/llm/default-workspace-cost-unattributable/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_default_workspace_cost.py
node node/anthropic-default-workspace-cost.mjs
```

## Test it

```bash
pytest python/test_anthropic_default_workspace_cost.py
node --test node/anthropic-default-workspace-cost.test.mjs
```
