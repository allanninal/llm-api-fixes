# spend jumped week over week and no release explains it

The invoice is three times last month's and nothing shipped. There is no error to look up, no status code, no failed request &mdash; the API worked perfectly all month, which is the problem. Somewhere in the last six weeks a cron went from hourly to every five minutes, or a prompt template grew a retrieved document, or a customer onboarded, and the feedback loop between that change and the bill is measured in weeks. By the time the number arrives, nobody remembers the week it started in.

**Full guide with diagrams:** https://www.allanninal.dev/llm/spend-spike-week-over-week/

## Run it

```bash
export DRY_RUN="true"   # report only, write nothing
python python/llm_spend_week_over_week.py
node node/llm-spend-week-over-week.mjs
```

## Test it

```bash
pytest python/test_llm_spend_week_over_week.py
node --test node/llm-spend-week-over-week.test.mjs
```
