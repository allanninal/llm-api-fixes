# web search is billing $10 per 1,000 searches unnoticed

The research agent was allowed to search the web in March, because an agent that cannot look anything up is a party trick. Nobody put a ceiling on how many times it could search in one turn, because in March it searched twice. It now runs on every support ticket, it searches until it is satisfied, and satisfied averages eleven. None of that is a token, so the token dashboard on the wall behind the standup has never once flickered.

**Full guide with diagrams:** https://www.allanninal.dev/llm/web-search-spend-unnoticed/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/anthropic_web_search_spend_audit.py
node node/anthropic-web-search-spend-audit.mjs
```

## Test it

```bash
pytest python/test_anthropic_web_search_spend_audit.py
node --test node/anthropic-web-search-spend-audit.test.mjs
```
