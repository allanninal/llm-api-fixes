# purpose=assistants files outlived the API that owned them

The Assistants API reached its shutdown date on 26 August 2026 and the migration was done months ago. Runs became responses, threads became conversations, the beta header came out of the client, and the whole thing has been working ever since. What nobody did, because nothing asked and nothing broke, was go and look at the files. They are still there. Every document ever uploaded for an assistant, every code-interpreter output from a run that no longer exists, sitting in a store whose owner was deleted, counting against a ceiling and answering every listing call as though the last two years never happened.

**Full guide with diagrams:** https://www.allanninal.dev/llm/orphaned-assistants-purpose-files/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_orphaned_assistant_files.py
node node/openai-orphaned-assistant-files.mjs
```

## Test it

```bash
pytest python/test_openai_orphaned_assistant_files.py
node --test node/openai-orphaned-assistant-files.test.mjs
```
