# an archived project still holds live API keys

The prototype was shut down in the spring. The project was archived, which felt like closing it: it vanished from the console's project switcher and from every list anyone looks at. Nothing inside it was touched. Two keys are still enabled, one of them authenticated a request last Tuesday, and the quarterly key audit has never seen either of them, because the audit iterates projects and archived projects are not in the default listing.

**Full guide with diagrams:** https://www.allanninal.dev/llm/archived-project-still-holds-keys/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_archived_project_keys.py
node node/openai-archived-project-keys.mjs
```

## Test it

```bash
pytest python/test_openai_archived_project_keys.py
node --test node/openai-archived-project-keys.test.mjs
```
