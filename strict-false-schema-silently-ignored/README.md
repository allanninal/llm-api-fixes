# strict omitted, so the JSON schema is only a suggestion

The validator threw about once every fifty calls, always in production, never in a test. Sometimes an extra key nobody had asked for; sometimes an optional field missing that the schema said was required; once a number arriving as a string. It read like flakiness, so it got a retry, and the retry mostly worked, which settled the matter for four months. The schema had been attached to every one of those calls. It had also never once been enforced, because somebody had taken the strict flag out eleven months earlier to make a 400 go away.

**Full guide with diagrams:** https://www.allanninal.dev/llm/strict-false-schema-silently-ignored/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/openai_advisory_schema.py
node node/openai-advisory-schema.mjs
```

## Test it

```bash
pytest python/test_openai_advisory_schema.py
node --test node/openai-advisory-schema.test.mjs
```
